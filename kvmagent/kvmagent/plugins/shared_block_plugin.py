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
from zstacklib.utils.plugin import completetask
import zstacklib.utils.uuidhelper as uuidhelper

logger = log.get_logger(__name__)
LOCK_FILE = "/var/run/zstack/sharedblock.lock"
INIT_TAG = "zs::sharedblock::init"
HEARTBEAT_TAG = "zs::sharedblock::heartbeat"
VOLUME_TAG = "zs::sharedblock::volume"
IMAGE_TAG = "zs::sharedblock::image"
DEFAULT_VG_METADATA_SIZE = "2g"
DEFAULT_SANLOCK_LV_SIZE = "1024"
QMP_SOCKET_PATH = "/var/lib/libvirt/qemu/zstack"


class AgentRsp(object):
    def __init__(self):
        self.success = True
        self.error = None
        self.totalCapacity = None
        self.availableCapacity = None


class ConnectRsp(AgentRsp):
    def __init__(self):
        super(ConnectRsp, self).__init__()
        self.isFirst = False
        self.hostId = None
        self.vgLvmUuid = None
        self.hostUuid = None


class RevertVolumeFromSnapshotRsp(AgentRsp):
    def __init__(self):
        super(RevertVolumeFromSnapshotRsp, self).__init__()
        self.newVolumeInstallPath = None
        self.size = None


class MergeSnapshotRsp(AgentRsp):
    def __init__(self):
        super(MergeSnapshotRsp, self).__init__()
        self.size = None
        self.actualSize = None


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


class OfflineMergeSnapshotRsp(AgentRsp):
    def __init__(self):
        super(OfflineMergeSnapshotRsp, self).__init__()
        self.deleted = False


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


class GetBackingChainRsp(AgentRsp):
    backingChain = None  # type: list[str]

    def __init__(self):
        super(GetBackingChainRsp, self).__init__()
        self.backingChain = None


class SharedBlockMigrateVolumeStruct:
    volumeUuid = None  # type: str
    snapshotUuid = None  # type: str
    currentInstallPath = None  # type: str
    targetInstallPath = None  # type: str
    safeMode = False
    compareQcow2 = True
    exists_lock = None

    def __init__(self):
        pass


class ConvertVolumeProvisioningRsp(AgentRsp):
    actualSize = None  # type: int

    def __init__(self):
        super(ConvertVolumeProvisioningRsp, self).__init__()
        self.actualSize = 0


def translate_absolute_path_from_install_path(path):
    if path is None:
        raise Exception("install path can not be null")
    return path.replace("sharedblock:/", "/dev")


def get_primary_storage_uuid_from_install_path(path):
    # type: (str) -> str
    if path is None:
        raise Exception("install path can not be null")
    return path.split("/")[2]


class CheckDisk(object):
    def __init__(self, identifier):
        self.identifier = identifier

    def get_path(self):
        o = self.check_disk_by_wwid()
        if o is not None:
            return o

        o = self.check_disk_by_uuid()
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

    def check_disk_by_uuid(self):
        for cond in ['TYPE=\\\"mpath\\\"', '\"\"']:
            cmd = shell.ShellCmd("lsblk --pair -p -o NAME,TYPE,FSTYPE,LABEL,UUID,VENDOR,MODEL,MODE,WWN | "
                                 " grep %s | grep %s | sort | uniq" % (cond, self.identifier))
            cmd(is_exception=False)
            if len(cmd.stdout.splitlines()) == 1:
                pattern = re.compile(r'\/dev\/[^ \"]*')
                return pattern.findall(cmd.stdout)[0]

    def check_disk_by_wwid(self):
        for cond in ['dm-uuid-mpath-', '']:
            cmd = shell.ShellCmd("readlink -e /dev/disk/by-id/%s%s" % (cond, self.identifier))
            cmd(is_exception=False)
            if cmd.return_code == 0:
                return cmd.stdout.strip()


class SharedBlockPlugin(kvmagent.KvmAgent):

    CONNECT_PATH = "/sharedblock/connect"
    DISCONNECT_PATH = "/sharedblock/disconnect"
    CREATE_VOLUME_FROM_CACHE_PATH = "/sharedblock/createrootvolume"
    DELETE_BITS_PATH = "/sharedblock/bits/delete"
    CREATE_TEMPLATE_FROM_VOLUME_PATH = "/sharedblock/createtemplatefromvolume"
    UPLOAD_BITS_TO_SFTP_BACKUPSTORAGE_PATH = "/sharedblock/sftp/upload"
    DOWNLOAD_BITS_FROM_SFTP_BACKUPSTORAGE_PATH = "/sharedblock/sftp/download"
    UPLOAD_BITS_TO_IMAGESTORE_PATH = "/sharedblock/imagestore/upload"
    COMMIT_BITS_TO_IMAGESTORE_PATH = "/sharedblock/imagestore/commit"
    DOWNLOAD_BITS_FROM_IMAGESTORE_PATH = "/sharedblock/imagestore/download"
    REVERT_VOLUME_FROM_SNAPSHOT_PATH = "/sharedblock/volume/revertfromsnapshot"
    MERGE_SNAPSHOT_PATH = "/sharedblock/snapshot/merge"
    OFFLINE_MERGE_SNAPSHOT_PATH = "/sharedblock/snapshot/offlinemerge"
    CREATE_EMPTY_VOLUME_PATH = "/sharedblock/volume/createempty"
    CHECK_BITS_PATH = "/sharedblock/bits/check"
    RESIZE_VOLUME_PATH = "/sharedblock/volume/resize"
    CONVERT_IMAGE_TO_VOLUME = "/sharedblock/image/tovolume"
    CHANGE_VOLUME_ACTIVE_PATH = "/sharedblock/volume/active"
    GET_VOLUME_SIZE_PATH = "/sharedblock/volume/getsize"
    CHECK_DISKS_PATH = "/sharedblock/disks/check"
    ADD_SHARED_BLOCK = "/sharedblock/disks/add"
    MIGRATE_DATA_PATH = "/sharedblock/volume/migrate"
    GET_BLOCK_DEVICES_PATH = "/sharedblock/blockdevices"
    DOWNLOAD_BITS_FROM_KVM_HOST_PATH = "/sharedblock/kvmhost/download"
    CANCEL_DOWNLOAD_BITS_FROM_KVM_HOST_PATH = "/sharedblock/kvmhost/download/cancel"
    GET_BACKING_CHAIN_PATH = "/sharedblock/volume/backingchain"
    CONVERT_VOLUME_PROVISIONING_PATH = "/sharedblock/volume/convertprovisioning"

    def start(self):
        http_server = kvmagent.get_http_server()
        http_server.register_async_uri(self.CONNECT_PATH, self.connect)
        http_server.register_async_uri(self.DISCONNECT_PATH, self.disconnect)
        http_server.register_async_uri(self.CREATE_VOLUME_FROM_CACHE_PATH, self.create_root_volume)
        http_server.register_async_uri(self.DELETE_BITS_PATH, self.delete_bits)
        http_server.register_async_uri(self.CREATE_TEMPLATE_FROM_VOLUME_PATH, self.create_template_from_volume)
        http_server.register_async_uri(self.UPLOAD_BITS_TO_SFTP_BACKUPSTORAGE_PATH, self.upload_to_sftp)
        http_server.register_async_uri(self.DOWNLOAD_BITS_FROM_SFTP_BACKUPSTORAGE_PATH, self.download_from_sftp)
        http_server.register_async_uri(self.UPLOAD_BITS_TO_IMAGESTORE_PATH, self.upload_to_imagestore)
        http_server.register_async_uri(self.COMMIT_BITS_TO_IMAGESTORE_PATH, self.commit_to_imagestore)
        http_server.register_async_uri(self.DOWNLOAD_BITS_FROM_IMAGESTORE_PATH, self.download_from_imagestore)
        http_server.register_async_uri(self.REVERT_VOLUME_FROM_SNAPSHOT_PATH, self.revert_volume_from_snapshot)
        http_server.register_async_uri(self.MERGE_SNAPSHOT_PATH, self.merge_snapshot)
        http_server.register_async_uri(self.OFFLINE_MERGE_SNAPSHOT_PATH, self.offline_merge_snapshots)
        http_server.register_async_uri(self.CREATE_EMPTY_VOLUME_PATH, self.create_empty_volume)
        http_server.register_async_uri(self.CONVERT_IMAGE_TO_VOLUME, self.convert_image_to_volume)
        http_server.register_async_uri(self.CHECK_BITS_PATH, self.check_bits)
        http_server.register_async_uri(self.RESIZE_VOLUME_PATH, self.resize_volume)
        http_server.register_async_uri(self.CHANGE_VOLUME_ACTIVE_PATH, self.active_lv)
        http_server.register_async_uri(self.GET_VOLUME_SIZE_PATH, self.get_volume_size)
        http_server.register_async_uri(self.CHECK_DISKS_PATH, self.check_disks)
        http_server.register_async_uri(self.ADD_SHARED_BLOCK, self.add_disk)
        http_server.register_async_uri(self.MIGRATE_DATA_PATH, self.migrate_volumes)
        http_server.register_async_uri(self.GET_BLOCK_DEVICES_PATH, self.get_block_devices)
        http_server.register_async_uri(self.DOWNLOAD_BITS_FROM_KVM_HOST_PATH, self.download_from_kvmhost)
        http_server.register_async_uri(self.CANCEL_DOWNLOAD_BITS_FROM_KVM_HOST_PATH, self.cancel_download_from_kvmhost)
        http_server.register_async_uri(self.GET_BACKING_CHAIN_PATH, self.get_backing_chain)
        http_server.register_async_uri(self.CONVERT_VOLUME_PROVISIONING_PATH, self.convert_volume_provisioning)

        self.imagestore_client = ImageStoreClient()

    def stop(self):
        pass

    @kvmagent.replyerror
    def check_disks(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        if cmd.failIfNoPath:
            linux.set_fail_if_no_path()
        for diskUuid in cmd.sharedBlockUuids:
            disk = CheckDisk(diskUuid)
            path = disk.get_path()
            if cmd.rescan:
                disk.rescan(path.split("/")[-1])

        if cmd.vgUuid is not None and lvm.vg_exists(cmd.vgUuid):
            rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid, False)

        return jsonobject.dumps(rsp)

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

            cmd = shell.ShellCmd("vgcreate -qq --shared --addtag '%s::%s::%s::%s' --metadatasize %s %s %s" %
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
    def connect(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ConnectRsp()
        diskPaths = set()

        def config_lvm(host_id, enableLvmetad=False):
            lvm.backup_lvm_config()
            lvm.reset_lvm_conf_default()
            lvm.config_lvm_by_sed("use_lvmlockd", "use_lvmlockd=1", ["lvm.conf", "lvmlocal.conf"])
            if enableLvmetad:
                lvm.config_lvm_by_sed("use_lvmetad", "use_lvmetad=1", ["lvm.conf", "lvmlocal.conf"])
            else:
                lvm.config_lvm_by_sed("use_lvmetad", "use_lvmetad=0", ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("host_id", "host_id=%s" % host_id, ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("sanlock_lv_extend", "sanlock_lv_extend=%s" % DEFAULT_SANLOCK_LV_SIZE, ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("lvmlockd_lock_retries", "lvmlockd_lock_retries=6", ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("issue_discards", "issue_discards=1", ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("reserved_stack", "reserved_stack=256", ["lvm.conf", "lvmlocal.conf"])
            lvm.config_lvm_by_sed("reserved_memory", "reserved_memory=131072", ["lvm.conf", "lvmlocal.conf"])

            lvm.config_lvm_filter(["lvm.conf", "lvmlocal.conf"])

            lvm.config_sanlock_by_sed("sh_retries", "sh_retries=20")
            lvm.config_sanlock_by_sed("logfile_priority", "logfile_priority=7")
            lvm.config_sanlock_by_sed("renewal_read_extend_sec", "renewal_read_extend_sec=24")
            lvm.config_sanlock_by_sed("debug_renew", "debug_renew=1")
            lvm.config_sanlock_by_sed("use_watchdog", "use_watchdog=0")

            sanlock_hostname = "%s-%s-%s" % (cmd.vgUuid[:8], cmd.hostUuid[:8], bash.bash_o("hostname").strip()[:20])
            lvm.config_sanlock_by_sed("our_host_name", "our_host_name=%s" % sanlock_hostname)

        config_lvm(cmd.hostId, cmd.enableLvmetad)
        for diskUuid in cmd.sharedBlockUuids:
            disk = CheckDisk(diskUuid)
            diskPaths.add(disk.get_path())
        lvm.start_lvmlockd()
        lvm.check_gl_lock()
        logger.debug("find/create vg %s lock..." % cmd.vgUuid)
        rsp.isFirst = self.create_vg_if_not_found(cmd.vgUuid, diskPaths, cmd.hostUuid, cmd.forceWipe)

        lvm.check_stuck_vglk()
        logger.debug("starting vg %s lock..." % cmd.vgUuid)
        lvm.start_vg_lock(cmd.vgUuid)

        if lvm.lvm_vgck(cmd.vgUuid, 60)[0] is False and lvm.lvm_check_operation(cmd.vgUuid) is False:
            lvm.drop_vg_lock(cmd.vgUuid)
            logger.debug("restarting vg %s lock..." % cmd.vgUuid)
            lvm.check_gl_lock()
            lvm.start_vg_lock(cmd.vgUuid)

        lvm.clean_vg_exists_host_tags(cmd.vgUuid, cmd.hostUuid, HEARTBEAT_TAG)
        lvm.add_vg_tag(cmd.vgUuid, "%s::%s::%s::%s" % (HEARTBEAT_TAG, cmd.hostUuid, time.time(), bash.bash_o('hostname').strip()))
        self.clear_stalled_qmp_socket()

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        rsp.hostId = lvm.get_running_host_id(cmd.vgUuid)
        rsp.vgLvmUuid = lvm.get_vg_lvm_uuid(cmd.vgUuid)
        rsp.hostUuid = cmd.hostUuid
        return jsonobject.dumps(rsp)

    @staticmethod
    @bash.in_bash
    def clear_stalled_qmp_socket():
        def get_used_qmp_file():
            t = bash.bash_o("ps aux | grep -Eo -- '-qmp unix:%s/\w*\.sock'" % QMP_SOCKET_PATH).splitlines()
            qmp = []
            for i in t:
                qmp.append(i.split("/")[-1])
            return qmp

        exists_qmp_files = set(bash.bash_o("ls %s" % QMP_SOCKET_PATH).splitlines())
        if len(exists_qmp_files) == 0:
            return

        running_qmp_files = set(get_used_qmp_file())
        if len(running_qmp_files) == 0:
            bash.bash_roe("/bin/rm %s/*" % QMP_SOCKET_PATH)
            return

        need_delete_qmp_files = exists_qmp_files.difference(running_qmp_files)
        if len(need_delete_qmp_files) == 0:
            return

        for f in need_delete_qmp_files:
            bash.bash_roe("/bin/rm %s/%s" % (QMP_SOCKET_PATH, f))

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

        try:
            find_vg(cmd.vgUuid)
        except RetryException:
            logger.debug("can not find vg %s; return success" % cmd.vgUuid)
            return jsonobject.dumps(rsp)
        except Exception as e:
            raise e

        @linux.retry(times=3, sleep_time=random.uniform(0.1, 3))
        def deactive_lvs_on_vg(vgUuid):
            active_lvs = lvm.list_local_active_lvs(vgUuid)
            if len(active_lvs) == 0:
                return
            logger.warn("active lvs %s will be deactivate" % active_lvs)
            lvm.deactive_lv(vgUuid)
            active_lvs = lvm.list_local_active_lvs(vgUuid)
            if len(active_lvs) != 0:
                raise RetryException("lvs [%s] still active, retry deactive again" % active_lvs)

        deactive_lvs_on_vg(cmd.vgUuid)
        lvm.clean_vg_exists_host_tags(cmd.vgUuid, cmd.hostUuid, HEARTBEAT_TAG)
        lvm.stop_vg_lock(cmd.vgUuid)
        if cmd.stopServices:
            lvm.quitLockServices()
        lvm.clean_lvm_archive_files(cmd.vgUuid)
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
        install_abs_path = translate_absolute_path_from_install_path(cmd.installPath)

        with lvm.RecursiveOperateLv(install_abs_path, shared=False):
            lvm.resize_lv_from_cmd(install_abs_path, cmd.size, cmd)
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
        template_abs_path_cache = translate_absolute_path_from_install_path(cmd.templatePathInCache)
        install_abs_path = translate_absolute_path_from_install_path(cmd.installPath)
        qcow2_options = self.calc_qcow2_option(self, cmd.qcow2Options, True, cmd.provisioning)

        with lvm.RecursiveOperateLv(template_abs_path_cache, shared=True, skip_deactivate_tags=[IMAGE_TAG]):
            virtual_size = linux.qcow2_virtualsize(template_abs_path_cache)
            if not lvm.lv_exists(install_abs_path):
                lvm.create_lv_from_cmd(install_abs_path, virtual_size, cmd,
                                                 "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            with lvm.OperateLv(install_abs_path, shared=False, delete_when_exception=True):
                linux.qcow2_clone_with_option(template_abs_path_cache, install_abs_path, qcow2_options)

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
        install_abs_path = translate_absolute_path_from_install_path(path)
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
        volume_abs_path = translate_absolute_path_from_install_path(cmd.volumePath)
        install_abs_path = translate_absolute_path_from_install_path(cmd.installPath)

        if cmd.sharedVolume:
            lvm.do_active_lv(volume_abs_path, lvm.LvmlockdLockType.SHARE, True)

        with lvm.RecursiveOperateLv(volume_abs_path, shared=cmd.sharedVolume, skip_deactivate_tags=[IMAGE_TAG]):
            virtual_size = linux.qcow2_virtualsize(volume_abs_path)
            total_size = 0
            compress = False
            for qcow2 in linux.qcow2_get_file_chain(volume_abs_path):
                if bash.bash_r("qemu-img check %s | grep compressed" % volume_abs_path) == 0:
                    compress = True
                total_size += int(lvm.get_lv_size(qcow2))

            if total_size > virtual_size:
                total_size = virtual_size

            if bash.bash_r("qemu-img info --backing-chain %s | grep compress" % volume_abs_path) == 0:
                compress = True

            if not lvm.lv_exists(install_abs_path):
                lvm.create_lv_from_absolute_path(install_abs_path, total_size,
                                                 "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            with lvm.OperateLv(install_abs_path, shared=False, delete_when_exception=True):
                linux.create_template(volume_abs_path, install_abs_path, compress)
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
        install_abs_path = translate_absolute_path_from_install_path(cmd.primaryStorageInstallPath)

        def upload():
            if not os.path.exists(cmd.primaryStorageInstallPath):
                raise kvmagent.KvmError('cannot find %s' % cmd.primaryStorageInstallPath)

            linux.scp_upload(cmd.hostname, cmd.sshKey, cmd.primaryStorageInstallPath, cmd.backupStorageInstallPath, cmd.username, cmd.sshPort)

        with lvm.OperateLv(install_abs_path, shared=True):
            upload()

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def download_from_sftp(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        install_abs_path = translate_absolute_path_from_install_path(cmd.primaryStorageInstallPath)

        self.do_download_from_sftp(cmd, install_abs_path)

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    def do_download_from_sftp(self, cmd, install_abs_path):
        if not lvm.lv_exists(install_abs_path):
            size = linux.sftp_get(cmd.hostname, cmd.sshKey, cmd.backupStorageInstallPath, install_abs_path, cmd.username, cmd.sshPort, True)
            lvm.create_lv_from_absolute_path(install_abs_path, size,
                                             "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))

        with lvm.OperateLv(install_abs_path, shared=False, delete_when_exception=True):
            linux.scp_download(cmd.hostname, cmd.sshKey, cmd.backupStorageInstallPath, install_abs_path, cmd.username, cmd.sshPort, cmd.bandWidth)
        logger.debug('successfully download %s/%s to %s' % (cmd.hostname, cmd.backupStorageInstallPath, cmd.primaryStorageInstallPath))

        self.do_active_lv(cmd.primaryStorageInstallPath, cmd.lockType, False)

    def cancel_download_from_sftp(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()

        install_abs_path = translate_absolute_path_from_install_path(cmd.primaryStorageInstallPath)
        shell.run("pkill -9 -f '%s'" % install_abs_path)

        self.do_delete_bits(cmd.primaryStorageInstallPath)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @completetask
    def download_from_kvmhost(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()

        install_abs_path = translate_absolute_path_from_install_path(cmd.primaryStorageInstallPath)

        # todo: assume agent will not restart, maybe need clean
        last_task = self.load_and_save_task(req, rsp, os.path.exists, install_abs_path)
        if last_task and last_task.agent_pid == os.getpid():
            rsp = self.wait_task_complete(last_task)
            return jsonobject.dumps(rsp)

        self.do_download_from_sftp(cmd, install_abs_path)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def cancel_download_from_kvmhost(self, req):
        return self.cancel_download_from_sftp(req)

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
        self.imagestore_client.download_from_imagestore(None, cmd.hostname, cmd.backupStorageInstallPath, cmd.primaryStorageInstallPath)
        self.do_active_lv(cmd.primaryStorageInstallPath, cmd.lockType, True)
        rsp = AgentRsp()
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def revert_volume_from_snapshot(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = RevertVolumeFromSnapshotRsp()
        snapshot_abs_path = translate_absolute_path_from_install_path(cmd.snapshotInstallPath)
        qcow2_options = self.calc_qcow2_option(self, cmd.qcow2Options, True, cmd.provisioning)
        new_volume_path = cmd.installPath
        if new_volume_path is None or new_volume_path == "":
            new_volume_path = "/dev/%s/%s" % (cmd.vgUuid, uuidhelper.uuid())
        else:
            new_volume_path = translate_absolute_path_from_install_path(new_volume_path)

        with lvm.RecursiveOperateLv(snapshot_abs_path, shared=True):
            size = linux.qcow2_virtualsize(snapshot_abs_path)

            lvm.create_lv_from_cmd(new_volume_path, size, cmd,
                                             "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            with lvm.OperateLv(new_volume_path, shared=False, delete_when_exception=True):
                linux.qcow2_clone_with_option(snapshot_abs_path, new_volume_path, qcow2_options)
                size = linux.qcow2_virtualsize(new_volume_path)

        rsp.newVolumeInstallPath = new_volume_path
        rsp.size = size
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def merge_snapshot(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = MergeSnapshotRsp()
        snapshot_abs_path = translate_absolute_path_from_install_path(cmd.snapshotInstallPath)
        workspace_abs_path = translate_absolute_path_from_install_path(cmd.workspaceInstallPath)

        with lvm.RecursiveOperateLv(snapshot_abs_path, shared=True):
            virtual_size = linux.qcow2_virtualsize(snapshot_abs_path)
            if not lvm.lv_exists(workspace_abs_path):
                lvm.create_lv_from_absolute_path(workspace_abs_path, virtual_size,
                                                 "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            with lvm.OperateLv(workspace_abs_path, shared=False, delete_when_exception=True):
                linux.create_template(snapshot_abs_path, workspace_abs_path)
                rsp.size, rsp.actualSize = linux.qcow2_size_and_actual_size(workspace_abs_path)

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        rsp.actualSize = rsp.size
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def offline_merge_snapshots(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = OfflineMergeSnapshotRsp()
        src_abs_path = translate_absolute_path_from_install_path(cmd.srcPath)
        dst_abs_path = translate_absolute_path_from_install_path(cmd.destPath)

        with lvm.RecursiveOperateLv(src_abs_path, shared=True):
            virtual_size = linux.qcow2_virtualsize(src_abs_path)
            if not lvm.lv_exists(dst_abs_path):
                lvm.create_lv_from_absolute_path(dst_abs_path, virtual_size,
                                                 "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            with lvm.RecursiveOperateLv(dst_abs_path, shared=False):
                if not cmd.fullRebase:
                    linux.qcow2_rebase(src_abs_path, dst_abs_path)
                else:
                    tmp_lv = 'tmp_%s' % uuidhelper.uuid()
                    tmp_abs_path = os.path.join(os.path.dirname(dst_abs_path), tmp_lv)
                    tmp_abs_path = os.path.join(os.path.dirname(dst_abs_path), tmp_lv)
                    logger.debug("creating temp lv %s" % tmp_abs_path)
                    lvm.create_lv_from_absolute_path(tmp_abs_path, virtual_size,
                                                     "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
                    with lvm.OperateLv(tmp_abs_path, shared=False, delete_when_exception=True):
                        linux.create_template(dst_abs_path, tmp_abs_path)
                        lvm.lv_rename(tmp_abs_path, dst_abs_path, overwrite=True)

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @lock.file_lock(LOCK_FILE)
    def create_empty_volume(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()

        install_abs_path = translate_absolute_path_from_install_path(cmd.installPath)

        if cmd.backingFile:
            qcow2_options = self.calc_qcow2_option(self, cmd.qcow2Options, True, cmd.provisioning)
            backing_abs_path = translate_absolute_path_from_install_path(cmd.backingFile)
            with lvm.RecursiveOperateLv(backing_abs_path, shared=True):
                virtual_size = linux.qcow2_virtualsize(backing_abs_path)

                if not lvm.lv_exists(install_abs_path):
                    lvm.create_lv_from_cmd(install_abs_path, virtual_size, cmd,
                                                     "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
                with lvm.OperateLv(install_abs_path, shared=False, delete_when_exception=True):
                    linux.qcow2_create_with_backing_file_and_option(backing_abs_path, install_abs_path, qcow2_options)
        elif not lvm.lv_exists(install_abs_path):
            lvm.create_lv_from_cmd(install_abs_path, cmd.size, cmd,
                                                 "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
            if cmd.volumeFormat != 'raw':
                qcow2_options = self.calc_qcow2_option(self, cmd.qcow2Options, False, cmd.provisioning)
                with lvm.OperateLv(install_abs_path, shared=False, delete_when_exception=True):
                    linux.qcow2_create_with_option(install_abs_path, cmd.size, qcow2_options)
                    linux.qcow2_fill(0, 1048576, install_abs_path)

        logger.debug('successfully create empty volume[uuid:%s, size:%s] at %s' % (cmd.volumeUuid, cmd.size, cmd.installPath))
        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def convert_image_to_volume(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()

        install_abs_path = translate_absolute_path_from_install_path(cmd.primaryStorageInstallPath)
        with lvm.OperateLv(install_abs_path, shared=False):
            lvm.clean_lv_tag(install_abs_path, IMAGE_TAG)
            lvm.add_lv_tag(install_abs_path, "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def check_bits(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = CheckBitsRsp()
        install_abs_path = translate_absolute_path_from_install_path(cmd.path)
        rsp.existing = lvm.lv_exists(install_abs_path)
        if cmd.vgUuid is not None and lvm.vg_exists(cmd.vgUuid):
            rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid, False)
        return jsonobject.dumps(rsp)

    def do_active_lv(self, installPath, lockType, recursive, killProcess=False):
        def handle_lv(lockType, fpath):
            if lockType > lvm.LvmlockdLockType.NULL:
                lvm.active_lv(fpath, lockType == lvm.LvmlockdLockType.SHARE)
            else:
                try:
                    lvm.deactive_lv(fpath)
                except Exception as e:
                    if not killProcess:
                        return
                    qemus = lvm.find_qemu_for_lv_in_use(fpath)
                    if len(qemus) == 0:
                        return
                    for qemu in qemus:
                        if qemu.state != "running":
                            linux.kill_process(qemu.pid)
                    lvm.deactive_lv(fpath)

        install_abs_path = translate_absolute_path_from_install_path(installPath)
        handle_lv(lockType, install_abs_path)

        if recursive is False or lockType is lvm.LvmlockdLockType.NULL:
            return

        while linux.qcow2_get_backing_file(install_abs_path) != "":
            install_abs_path = linux.qcow2_get_backing_file(install_abs_path)
            if lockType == lvm.LvmlockdLockType.NULL:
                handle_lv(lvm.LvmlockdLockType.NULL, install_abs_path)
            else:
                # activate backing files only in shared mode
                handle_lv(lvm.LvmlockdLockType.SHARE, install_abs_path)

    @kvmagent.replyerror
    def active_lv(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()
        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid, raise_exception=False)

        self.do_active_lv(cmd.installPath, cmd.lockType, cmd.recursive, cmd.killProcess)

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def get_volume_size(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = GetVolumeSizeRsp()

        install_abs_path = translate_absolute_path_from_install_path(cmd.installPath)
        with lvm.OperateLv(install_abs_path, shared=True):
            rsp.size = linux.qcow2_virtualsize(install_abs_path)
        rsp.actualSize = lvm.get_lv_size(install_abs_path)
        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @bash.in_bash
    def migrate_volumes(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = AgentRsp()

        for struct in cmd.migrateVolumeStructs:
            target_abs_path = translate_absolute_path_from_install_path(struct.targetInstallPath)
            current_abs_path = translate_absolute_path_from_install_path(struct.currentInstallPath)
            with lvm.OperateLv(current_abs_path, shared=True):
                lv_size = lvm.get_lv_size(current_abs_path)

                if lvm.lv_exists(target_abs_path):
                    target_ps_uuid = get_primary_storage_uuid_from_install_path(struct.targetInstallPath)
                    raise Exception("found %s already exists on ps %s" %
                                    (target_abs_path, target_ps_uuid))
                lvm.create_lv_from_absolute_path(target_abs_path, lvm.getOriginalSize(lv_size),
                                                     "%s::%s::%s" % (VOLUME_TAG, cmd.hostUuid, time.time()))
                lvm.active_lv(target_abs_path, lvm.LvmlockdLockType.SHARE)

        try:
            for struct in cmd.migrateVolumeStructs:
                target_abs_path = translate_absolute_path_from_install_path(struct.targetInstallPath)
                current_abs_path = translate_absolute_path_from_install_path(struct.currentInstallPath)

                with lvm.OperateLv(current_abs_path, shared=True):
                    bash.bash_errorout("cp %s %s" % (current_abs_path, target_abs_path))

            for struct in cmd.migrateVolumeStructs:
                target_abs_path = translate_absolute_path_from_install_path(struct.targetInstallPath)
                current_abs_path = translate_absolute_path_from_install_path(struct.currentInstallPath)
                with lvm.RecursiveOperateLv(current_abs_path, shared=True):
                    previous_ps_uuid = get_primary_storage_uuid_from_install_path(struct.currentInstallPath)
                    target_ps_uuid = get_primary_storage_uuid_from_install_path(struct.targetInstallPath)

                    current_backing_file = linux.qcow2_get_backing_file(current_abs_path)  # type: str
                    target_backing_file = current_backing_file.replace(previous_ps_uuid, target_ps_uuid)

                    if struct.compareQcow2:
                        logger.debug("comparing qcow2 between %s and %s" % (current_abs_path, target_abs_path))
                        if not self.compare(current_abs_path, target_abs_path):
                            raise Exception("qcow2 %s and %s are not identical" % (current_abs_path, target_abs_path))
                        logger.debug("confirmed qcow2 %s and %s are identical" % (current_abs_path, target_abs_path))
                    if current_backing_file is not None and current_backing_file != "":
                        lvm.do_active_lv(target_backing_file, lvm.LvmlockdLockType.SHARE, False)
                        logger.debug("rebase %s to %s" % (target_abs_path, target_backing_file))
                        linux.qcow2_rebase_no_check(target_backing_file, target_abs_path)
        except Exception as e:
            for struct in cmd.migrateVolumeStructs:
                target_abs_path = translate_absolute_path_from_install_path(struct.targetInstallPath)
                if struct.currentInstallPath == struct.targetInstallPath:
                    logger.debug("current install path %s equals target %s, skip to delete" %
                                 (struct.currentInstallPath, struct.targetInstallPath))
                else:
                    logger.debug("error happened, delete lv %s" % target_abs_path)
                    lvm.delete_lv(target_abs_path, False)
            raise e
        finally:
            for struct in cmd.migrateVolumeStructs:
                target_abs_path = translate_absolute_path_from_install_path(struct.targetInstallPath)
                lvm.deactive_lv(target_abs_path)

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @staticmethod
    def calc_qcow2_option(self, options, has_backing_file, provisioning=None):
        if options is None or options == "":
            return " "
        if has_backing_file or provisioning == lvm.VolumeProvisioningStrategy.ThinProvisioning:
            return re.sub("-o preallocation=\w* ", " ", options)
        return options

    @kvmagent.replyerror
    def get_block_devices(self, req):
        rsp = GetBlockDevicesRsp()
        rsp.blockDevices = lvm.get_block_devices()
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def get_backing_chain(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = GetBackingChainRsp()
        abs_path = translate_absolute_path_from_install_path(cmd.installPath)

        with lvm.RecursiveOperateLv(abs_path, shared=True, skip_deactivate_tags=[IMAGE_TAG], delete_when_exception=False):
            rsp.backingChain = linux.qcow2_get_file_chain(abs_path)

        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @bash.in_bash
    def convert_volume_provisioning(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ConvertVolumeProvisioningRsp()

        if cmd.provisioningStrategy != "ThinProvisioning":
            raise NotImplementedError

        abs_path = translate_absolute_path_from_install_path(cmd.installPath)
        with lvm.RecursiveOperateLv(abs_path, shared=False):
            image_offest = long(
                bash.bash_o("qemu-img check %s | grep 'Image end offset' | awk -F ': ' '{print $2}'" % abs_path).strip())
            current_size = long(lvm.get_lv_size(abs_path))
            virtual_size = linux.qcow2_virtualsize(abs_path)
            size = image_offest + cmd.addons[lvm.thinProvisioningInitializeSize]
            if size > current_size:
                size = current_size
            if size > virtual_size:
                size = virtual_size
            lvm.resize_lv(abs_path, size, True)

        rsp.actualSize = size
        rsp.totalCapacity, rsp.availableCapacity = lvm.get_vg_size(cmd.vgUuid)
        return jsonobject.dumps(rsp)
