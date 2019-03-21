import os.path
import re
import random
import time

from kvmagent import kvmagent
from kvmagent.plugins.imagestore import ImageStoreClient
from zstacklib.utils import jsonobject
from zstacklib.utils import http
from zstacklib.utils import log
from zstacklib.utils import shell
from zstacklib.utils import linux
from zstacklib.utils import lock
from zstacklib.utils import lvm
from zstacklib.utils import bash
from zstacklib.utils import drbd
from zstacklib.utils.plugin import completetask

logger = log.get_logger(__name__)
LOCK_FILE = "/var/run/zstack/ministorage.lock"
INIT_TAG = "zs::ministorage::init"
HEARTBEAT_TAG = "zs::ministorage::heartbeat"
VOLUME_TAG = "zs::ministorage::volume"
IMAGE_TAG = "zs::ministorage::image"
DEFAULT_VG_METADATA_SIZE = "1g"

INIT_POOL_RATIO = 0.1
DEFAULT_CHUNK_SIZE = "4194304"
DRBD_START_PORT = 20000


class AgentRsp(object):
    def __init__(self):
        self.success = True
        self.error = None
        self.totalCapacity = None
        self.availableCapacity = None


class ConnectRsp(AgentRsp):
    def __init__(self):
        super(ConnectRsp, self).__init__()
        self.hostUuid = None


class VolumeRsp(AgentRsp):
    def __init__(self):
        super(VolumeRsp, self).__init__()
        self.actualSize = None
        self.resourceUuid = None
        self.localRole = None
        self.localDiskStatus = None
        self.localNetworkStatus = None
        self.remoteRole = None
        self.remoteDiskStatus = None
        self.remoteNetworkStatus = None


class CheckBitsRsp(AgentRsp):
    def __init__(self):
        super(CheckBitsRsp, self).__init__()
        self.existing = False


class GetVolumeSizeRsp(AgentRsp):
    def __init__(self):
        super(GetVolumeSizeRsp, self).__init__()
        self.size = None
        self.actualSize = None


class ResizeVolumeRsp(AgentRsp):
    def __init__(self):
        super(ResizeVolumeRsp, self).__init__()
        self.size = None


class GetVolumeSizeRsp(AgentRsp):
    def __init__(self):
        super(GetVolumeSizeRsp, self).__init__()
        self.size = None


class RetryException(Exception):
    pass


class GetBlockDevicesRsp(AgentRsp):
    blockDevices = None  # type: list[lvm.SharedBlockCandidateStruct]

    def __init__(self):
        super(GetBlockDevicesRsp, self).__init__()
        self.blockDevices = None


class ConvertVolumeProvisioningRsp(AgentRsp):
    actualSize = None  # type: int

    def __init__(self):
        super(ConvertVolumeProvisioningRsp, self).__init__()
        self.actualSize = 0


def get_absolute_path_from_install_path(path):
    if path is None:
        raise Exception("install path can not be null")
    return path.replace("mini:/", "/dev")


def get_primary_storage_uuid_from_install_path(path):
    # type: (str) -> str
    if path is None:
        raise Exception("install path can not be null")
    return path.split("/")[2]


class CheckDisk(object):
    def __init__(self, identifier):
        self.identifier = identifier

    @bash.in_bash
    def check_disk_by_path(self):
        if bash.bash_r("ls %s" % self.identifier) == 0:
            return self.identifier
        return None

    def get_path(self):
        o = self.check_disk_by_path()
        if o is not None:
            return o

        raise Exception("can not find disk with %s as wwid, uuid or wwn, "
                        "or multiple disks qualify but no mpath device found" % self.identifier)

    @bash.in_bash
    def rescan(self, disk_name=None):
        """

        :type disk_name: str
        """
        if disk_name is None:
            disk_name = self.get_path().split("/")[-1]

        def rescan_slave(slave, raise_exception=True):
            _cmd = shell.ShellCmd("echo 1 > /sys/block/%s/device/rescan" % slave)
            _cmd(is_exception=raise_exception)
            logger.debug("rescaned disk %s (wwid: %s), return code: %s, stdout %s, stderr: %s" %
                         (slave, self.identifier, _cmd.return_code, _cmd.stdout, _cmd.stderr))

        multipath_dev = lvm.get_multipath_dmname(disk_name)
        if multipath_dev:
            t, disk_name = disk_name, multipath_dev
            # disk name is dm-xx when multi path
            slaves = shell.call("ls /sys/class/block/%s/slaves/" % disk_name).strip().split("\n")
            if slaves is None or len(slaves) == 0 or (len(slaves) == 1 and slaves[0].strip() == ""):
                logger.debug("can not get any slaves of multipath device %s" % disk_name)
                rescan_slave(disk_name, False)
            else:
                for s in slaves:
                    rescan_slave(s)
                cmd = shell.ShellCmd("multipathd resize map %s" % disk_name)
                cmd(is_exception=True)
                logger.debug("resized multipath device %s, return code: %s, stdout %s, stderr: %s" %
                             (disk_name, cmd.return_code, cmd.stdout, cmd.stderr))
            disk_name = t
        else:
            rescan_slave(disk_name)

        command = "pvresize /dev/%s" % disk_name
        if multipath_dev is not None and multipath_dev != disk_name:
            command = "pvresize /dev/%s || pvresize /dev/%s" % (disk_name, multipath_dev)
        r, o, e = bash.bash_roe(command, errorout=True)
        logger.debug("resized pv %s (wwid: %s), return code: %s, stdout %s, stderr: %s" %
                     (disk_name, self.identifier, r, o, e))

    def set_fail_if_no_path(self):
        if not lvm.is_multipath_running():
            return
        cmd = shell.ShellCmd('ms=`multipath -l -v1`; for m in $ms; do dmsetup message $m 0 "fail_if_no_path"; done')
        cmd(is_exception=False)


class MiniStoragePlugin(kvmagent.KvmAgent):

    CONNECT_PATH = "/ministorage/connect"
    DISCONNECT_PATH = "/ministorage/disconnect"
    CREATE_VOLUME_FROM_CACHE_PATH = "/ministorage/createrootvolume"
    DELETE_BITS_PATH = "/ministorage/bits/delete"
    CREATE_TEMPLATE_FROM_VOLUME_PATH = "/ministorage/createtemplatefromvolume"
    UPLOAD_BITS_TO_IMAGESTORE_PATH = "/ministorage/imagestore/upload"
    COMMIT_BITS_TO_IMAGESTORE_PATH = "/ministorage/imagestore/commit"
    DOWNLOAD_BITS_FROM_IMAGESTORE_PATH = "/ministorage/imagestore/download"
    CREATE_EMPTY_VOLUME_PATH = "/ministorage/volume/createempty"
    CHECK_BITS_PATH = "/ministorage/bits/check"
    RESIZE_VOLUME_PATH = "/ministorage/volume/resize"
    CONVERT_IMAGE_TO_VOLUME = "/ministorage/image/tovolume"
    CHANGE_VOLUME_ACTIVE_PATH = "/ministorage/volume/active"
    GET_VOLUME_SIZE_PATH = "/ministorage/volume/getsize"
    CHECK_DISKS_PATH = "/ministorage/disks/check"
    MIGRATE_DATA_PATH = "/ministorage/volume/migrate"

    def start(self):
        http_server = kvmagent.get_http_server()
        http_server.register_async_uri(self.CONNECT_PATH, self.connect)
        http_server.register_async_uri(self.DISCONNECT_PATH, self.disconnect)
        http_server.register_async_uri(self.CREATE_VOLUME_FROM_CACHE_PATH, self.create_root_volume)
        http_server.register_async_uri(self.DELETE_BITS_PATH, self.delete_bits)
        http_server.register_async_uri(self.CREATE_TEMPLATE_FROM_VOLUME_PATH, self.create_template_from_volume)
        http_server.register_async_uri(self.UPLOAD_BITS_TO_IMAGESTORE_PATH, self.upload_to_imagestore)
        http_server.register_async_uri(self.COMMIT_BITS_TO_IMAGESTORE_PATH, self.commit_to_imagestore)
        http_server.register_async_uri(self.DOWNLOAD_BITS_FROM_IMAGESTORE_PATH, self.download_from_imagestore)
        http_server.register_async_uri(self.CREATE_EMPTY_VOLUME_PATH, self.create_empty_volume)
        http_server.register_async_uri(self.CONVERT_IMAGE_TO_VOLUME, self.convert_image_to_volume)
        http_server.register_async_uri(self.CHECK_BITS_PATH, self.check_bits)
        http_server.register_async_uri(self.RESIZE_VOLUME_PATH, self.resize_volume)
        http_server.register_async_uri(self.CHANGE_VOLUME_ACTIVE_PATH, self.active_lv)
        http_server.register_async_uri(self.GET_VOLUME_SIZE_PATH, self.get_volume_size)
        http_server.register_async_uri(self.CHECK_DISKS_PATH, self.check_disks)

        self.imagestore_client = ImageStoreClient()

    def stop(self):
        pass

    @kvmagent.replyerror
    def check_disks(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        for diskId in cmd.diskIdentifiers:
            disk = CheckDisk(diskId)
            path = disk.get_path()
            if cmd.rescan:
                disk.rescan(path.split("/")[-1])
            if cmd.failIfNoPath:
                disk.set_fail_if_no_path()

        if cmd.vgUuid is not None and lvm.vg_exists(cmd.vgUuid):
            rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid, False)

        return jsonobject.dumps(rsp)

    @staticmethod
    @bash.in_bash
    def create_thin_pool_if_not_found(vgUuid, init_pool_ratio):
        def round_sector(size, sector):
            return round(float(size) / float(sector)) * sector

        if lvm.lv_exists("/dev/%s/%s_thinpool" % (vgUuid, vgUuid)):
            return
        tot, avil = lvm.get_vg_size(vgUuid)
        init_pool_size = float(tot) * float(init_pool_ratio)
        # meta_size = "%s" % ((tot / DEFAULT_CHUNK_SIZE) * 48 * 2)  # ref: https://www.kernel.org/doc/Documentation/device-mapper/thin-provisioning.txt
        meta_size = 1024**3  # ref: https://www.systutorials.com/docs/linux/man/7-lvmthin/#lbBD
        bash.bash_errorout("lvcreate --type thin-pool -L %sB -c %sB --poolmetadatasize %sB -n %s_thinpool %s" %
                           (int(round_sector(init_pool_size, 4096)), DEFAULT_CHUNK_SIZE, meta_size, vgUuid, vgUuid))

    @staticmethod
    def create_vg_if_not_found(vgUuid, diskPaths, hostUuid, forceWipe=False):
        @linux.retry(times=5, sleep_time=random.uniform(0.1, 3))
        def find_vg(vgUuid, raise_exception = True):
            cmd = shell.ShellCmd("timeout 5 vgscan --ignorelockingfailure; vgs --nolocking %s -otags | grep %s" % (vgUuid, INIT_TAG))
            cmd(is_exception=False)
            if cmd.return_code != 0 and raise_exception:
                raise RetryException("can not find vg %s with tag %s" % (vgUuid, INIT_TAG))
            elif cmd.return_code != 0:
                return False
            return True

        try:
            find_vg(vgUuid)
        except RetryException as e:
            if forceWipe is True:
                lvm.wipe_fs(diskPaths, vgUuid)

            cmd = shell.ShellCmd("vgcreate -qq --addtag '%s::%s::%s::%s' --metadatasize %s %s %s" %
                                 (INIT_TAG, hostUuid, time.time(), bash.bash_o("hostname").strip(),
                                  DEFAULT_VG_METADATA_SIZE, vgUuid, " ".join(diskPaths)))
            cmd(is_exception=False)
            logger.debug("created vg %s, ret: %s, stdout: %s, stderr: %s" %
                         (vgUuid, cmd.return_code, cmd.stdout, cmd.stderr))
            if cmd.return_code == 0 and find_vg(vgUuid, False) is True:
                return True
            try:
                if find_vg(vgUuid) is True:
                    return True
            except RetryException as ee:
                raise Exception("can not find vg %s with disks: %s and create vg return: %s %s %s " %
                                (vgUuid, diskPaths, cmd.return_code, cmd.stdout, cmd.stderr))
            except Exception as ee:
                raise ee
        except Exception as e:
            raise e

        return False

    @kvmagent.replyerror
    @lock.file_lock(LOCK_FILE)
    # TODO(weiw): config the global config
    def connect(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ConnectRsp()
        diskPaths = set()

        def config_lvm(enableLvmetad=False):
            lvm.backup_lvm_config()
            lvm.reset_lvm_conf_default()
            if enableLvmetad:
                lvm.config_lvm_by_sed("use_lvmetad", "use_lvmetad=1", ["lvm.conf", "lvmlocal.conf"])
            else:
                lvm.config_lvm_by_sed("use_lvmetad", "use_lvmetad=0", ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("issue_discards", "issue_discards=1", ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("reserved_stack", "reserved_stack=256", ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("reserved_memory", "reserved_memory=131072", ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("thin_pool_autoextend_threshold", "thin_pool_autoextend_threshold=80", ["lvm.conf", "lvmlocal.conf"])

            lvm.config_lvm_filter(["lvm.conf", "lvmlocal.conf"])

        drbd.install_drbd()
        config_lvm()
        for diskId in cmd.diskIdentifiers:
            disk = CheckDisk(diskId)
            diskPaths.add(disk.get_path())
        logger.debug("find/create vg %s ..." % cmd.vgUuid)
        self.create_vg_if_not_found(cmd.vgUuid, diskPaths, cmd.hostUuid, cmd.forceWipe)
        self.create_thin_pool_if_not_found(cmd.vgUuid, INIT_POOL_RATIO)
        drbd.up_all_resouces()

        if lvm.lvm_check_operation(cmd.vgUuid) is False:
            logger.warn("lvm operation test failed!")

        lvm.clean_vg_exists_host_tags(cmd.vgUuid, cmd.hostUuid, HEARTBEAT_TAG)
        lvm.add_vg_tag(cmd.vgUuid, "%s::%s::%s::%s" % (HEARTBEAT_TAG, cmd.hostUuid, time.time(), bash.bash_o('hostname').strip()))

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        rsp.vgLvmUuid = lvm.get_vg_lvm_uuid(cmd.vgUuid)
        rsp.hostUuid = cmd.hostUuid
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @lock.file_lock(LOCK_FILE)
    def disconnect(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()

        @linux.retry(times=3, sleep_time=random.uniform(0.1, 3))
        def find_vg(vgUuid):
            cmd = shell.ShellCmd("vgs --nolocking %s -otags | grep %s" % (vgUuid, INIT_TAG))
            cmd(is_exception=False)
            if cmd.return_code == 0:
                return True

            logger.debug("can not find vg %s with tag %s" % (vgUuid, INIT_TAG))
            cmd = shell.ShellCmd("vgs %s" % vgUuid)
            cmd(is_exception=False)
            if cmd.return_code == 0:
                logger.warn("found vg %s without tag %s" % (vgUuid, INIT_TAG))
                return True

            raise RetryException("can not find vg %s with or without tag %s" % (vgUuid, INIT_TAG))

        @linux.retry(times=3, sleep_time=random.uniform(0.1, 3))
        def deactive_drbd_resouces_on_vg(vgUuid):
            active_lvs = lvm.list_local_active_lvs(vgUuid)
            if len(active_lvs) == 0:
                return
            # drbd_resouces = blahblah
            logger.warn("active lvs %s will be deactivate" % active_lvs)
            lvm.deactive_lv(vgUuid)
            active_lvs = lvm.list_local_active_lvs(vgUuid)
            if len(active_lvs) != 0:
                raise RetryException("lvs [%s] still active, retry deactive again" % active_lvs)

        try:
            find_vg(cmd.vgUuid)
        except RetryException:
            logger.debug("can not find vg %s; return success" % cmd.vgUuid)
            return jsonobject.dumps(rsp)
        except Exception as e:
            raise e

        deactive_drbd_resouces_on_vg(cmd.vgUuid)
        lvm.clean_vg_exists_host_tags(cmd.vgUuid, cmd.hostUuid, HEARTBEAT_TAG)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @lock.file_lock(LOCK_FILE)
    def add_disk(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        disk = CheckDisk(cmd.diskUuid)
        command = shell.ShellCmd("vgs --nolocking %s -otags | grep %s" % (cmd.vgUuid, INIT_TAG))
        command(is_exception=False)
        if command.return_code != 0:
            self.create_vg_if_not_found(cmd.vgUuid, [disk.get_path()], cmd.hostUuid, cmd.forceWipe)
        else:
            lvm.check_gl_lock()
            if cmd.forceWipe is True:
                lvm.wipe_fs([disk.get_path()], cmd.vgUuid)
            lvm.add_pv(cmd.vgUuid, disk.get_path(), DEFAULT_VG_METADATA_SIZE)

        rsp = AgentRsp
        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def resize_volume(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        install_abs_path = get_absolute_path_from_install_path(cmd.installPath)

        lvm.resize_lv_from_cmd(install_abs_path, cmd.size, cmd)

        # find drbd volume, resize, do some things if remote connect via storage but not resize

        if not cmd.live:
            shell.call("qemu-img resize %s %s" % (install_abs_path, cmd.size))
        ret = linux.qcow2_virtualsize(install_abs_path)

        rsp = ResizeVolumeRsp()
        rsp.size = ret
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @lock.file_lock(LOCK_FILE)
    def create_root_volume(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        template_abs_path_cache = get_absolute_path_from_install_path(cmd.templatePathInCache)
        install_abs_path = get_absolute_path_from_install_path(cmd.installPath)

        drbdResource = drbd.DrbdResource(self.get_name_from_installPath(cmd.installPath), False)
        drbdResource.config.local_host.hostname = cmd.local_host_name
        drbdResource.config.local_host.disk = install_abs_path
        drbdResource.config.local_host.minor = cmd.local_host_port - DRBD_START_PORT
        drbdResource.config.local_host.address = "%s:%s" % (cmd.local_address, cmd.local_host_port)

        drbdResource.config.remote_host.hostname = cmd.remote_host_name
        drbdResource.config.remote_host.disk = install_abs_path
        drbdResource.config.remote_host.minor = cmd.remote_host_port - DRBD_START_PORT
        drbdResource.config.remote_host.address = "%s:%s" % (cmd.remote_address, cmd.remote_host_port)

        drbdResource.config.write_config()
        virtual_size = linux.qcow2_virtualsize(template_abs_path_cache)
        if not lvm.lv_exists(install_abs_path):
            lvm.create_lv_from_cmd(install_abs_path, virtual_size, cmd,
                                             "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
        lvm.active_lv(install_abs_path)
        drbdResource.initialize(cmd.init, cmd, template_abs_path_cache)

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def delete_bits(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        if cmd.folder:
            raise Exception("not support this operation")

        self.do_delete_bits(cmd.path)

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    def do_delete_bits(self, path):
        install_abs_path = get_absolute_path_from_install_path(path)
        if lvm.has_lv_tag(install_abs_path, IMAGE_TAG):
            logger.info('deleting lv image: ' + install_abs_path)
            lvm.delete_image(install_abs_path, IMAGE_TAG)
        else:
            logger.info('deleting lv volume: ' + install_abs_path)
            lvm.delete_lv(install_abs_path)

    @kvmagent.replyerror
    def create_template_from_volume(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        volume_abs_path = get_absolute_path_from_install_path(cmd.volumePath)
        install_abs_path = get_absolute_path_from_install_path(cmd.installPath)

        if cmd.sharedVolume:
            lvm.do_active_lv(volume_abs_path, lvm.LvmlockdLockType.SHARE, True)

        with lvm.RecursiveOperateLv(volume_abs_path, shared=cmd.sharedVolume, skip_deactivate_tags=[IMAGE_TAG]):
            virtual_size = linux.qcow2_virtualsize(volume_abs_path)
            total_size = 0
            for qcow2 in linux.qcow2_get_file_chain(volume_abs_path):
                total_size += int(lvm.get_lv_size(qcow2))

            if total_size > virtual_size:
                total_size = virtual_size

            if not lvm.lv_exists(install_abs_path):
                lvm.create_lv_from_absolute_path(install_abs_path, total_size,
                                                 "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            with lvm.OperateLv(install_abs_path, shared=False, delete_when_exception=True):
                linux.create_template(volume_abs_path, install_abs_path)
                logger.debug('successfully created template[%s] from volume[%s]' % (cmd.installPath, cmd.volumePath))
                if cmd.compareQcow2 is True:
                    logger.debug("comparing qcow2 between %s and %s")
                    bash.bash_errorout("time qemu-img compare %s %s" % (volume_abs_path, install_abs_path))
                    logger.debug("confirmed qcow2 %s and %s are identical" % (volume_abs_path, install_abs_path))

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @staticmethod
    @bash.in_bash
    def compare(src, dst):
        return bash.bash_r("cmp %s %s" % (src, dst)) == 0

    @kvmagent.replyerror
    def upload_to_sftp(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        install_abs_path = get_absolute_path_from_install_path(cmd.primaryStorageInstallPath)

        def upload():
            if not os.path.exists(cmd.primaryStorageInstallPath):
                raise kvmagent.KvmError('cannot find %s' % cmd.primaryStorageInstallPath)

            linux.scp_upload(cmd.hostname, cmd.sshKey, cmd.primaryStorageInstallPath, cmd.backupStorageInstallPath, cmd.username, cmd.sshPort)

        with lvm.OperateLv(install_abs_path, shared=True):
            upload()

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def upload_to_imagestore(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        return self.imagestore_client.upload_to_imagestore(cmd, req)

    @kvmagent.replyerror
    def commit_to_imagestore(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        return self.imagestore_client.commit_to_imagestore(cmd, req)

    @kvmagent.replyerror
    def download_from_imagestore(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        self.imagestore_client.download_from_imagestore(cmd.mountPoint, cmd.hostname, cmd.backupStorageInstallPath, cmd.primaryStorageInstallPath)
        rsp = AgentRsp()
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @lock.file_lock(LOCK_FILE)
    def create_empty_volume(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()

        install_abs_path = get_absolute_path_from_install_path(cmd.installPath)
        drbdResource = drbd.DrbdResource(self.get_name_from_installPath(cmd.installPath), False)
        drbdResource.config.local_host.hostname = cmd.local_host_name
        drbdResource.config.local_host.disk = install_abs_path
        drbdResource.config.local_host.minor = cmd.local_host_port - DRBD_START_PORT
        drbdResource.config.local_host.address = "%s:%s" % (cmd.local_address, cmd.local_host_port)

        drbdResource.config.remote_host.hostname = cmd.remote_host_name
        drbdResource.config.remote_host.disk = install_abs_path
        drbdResource.config.remote_host.minor = cmd.remote_host_port - DRBD_START_PORT
        drbdResource.config.remote_host.address = "%s:%s" % (cmd.remote_address, cmd.remote_host_port)

        drbdResource.config.write_config()

        if cmd.backingFile:
            backing_abs_path = get_absolute_path_from_install_path(cmd.backingFile)
            virtual_size = linux.qcow2_virtualsize(backing_abs_path)

            lvm.create_lv_from_cmd(install_abs_path, virtual_size, cmd,
                                                 "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            lvm.active_lv(install_abs_path)
            drbdResource.initialize(cmd.init, cmd, backing_abs_path)
        elif not lvm.lv_exists(install_abs_path):
            lvm.create_lv_from_cmd(install_abs_path, cmd.size, cmd,
                                                 "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            lvm.active_lv(install_abs_path)
            drbdResource.initialize(cmd.init, cmd)

        logger.debug('successfully create empty volume[uuid:%s, size:%s] at %s' % (cmd.volumeUuid, cmd.size, cmd.installPath))
        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @staticmethod
    def get_name_from_installPath(path):
        return path.split("/")[3]

    @kvmagent.replyerror
    def convert_image_to_volume(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()

        install_abs_path = get_absolute_path_from_install_path(cmd.primaryStorageInstallPath)
        with lvm.OperateLv(install_abs_path, shared=False):
            lvm.clean_lv_tag(install_abs_path, IMAGE_TAG)
            lvm.add_lv_tag(install_abs_path, "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def check_bits(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CheckBitsRsp()
        install_abs_path = get_absolute_path_from_install_path(cmd.path)
        rsp.existing = lvm.lv_exists(install_abs_path)
        if cmd.vgUuid is not None and lvm.vg_exists(cmd.vgUuid):
            rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid, False)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def active_lv(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid, raise_exception=False)

        drbdResource = drbd.DrbdResource(self.get_name_from_installPath(cmd.installPath))
        if cmd.role == drbd.DrbdRole.Primary:
            drbdResource.promote()
        else:
            drbdResource.demote()

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def get_volume_size(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = GetVolumeSizeRsp()

        install_abs_path = get_absolute_path_from_install_path(cmd.installPath)
        r = drbd.DrbdResource(cmd.volumeUuid)
        with drbd.OperateDrbd(r):
            rsp.size = linux.qcow2_virtualsize(r.get_dev_path())
        rsp.actualSize = lvm.get_lv_size(install_abs_path)
        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @staticmethod
    def calc_qcow2_option(self, options, has_backing_file, provisioning=None):
        if options is None or options == "":
            return " "
        if has_backing_file or provisioning == lvm.VolumeProvisioningStrategy.ThinProvisioning:
            return re.sub("-o preallocation=\w* ", " ", options)
        return options
