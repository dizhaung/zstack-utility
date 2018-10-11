__author__ = 'frank'

import os
import os.path
import pprint
import traceback
import fcntl
import shutil
import socket
import struct
from netaddr import IPNetwork

import zstacklib.utils.daemon as daemon
import zstacklib.utils.http as http
import zstacklib.utils.jsonobject as json_object
from zstacklib.utils.bash import *
from imagestore import ImageStoreClient

logger = log.get_logger(__name__)


class AgentResponse(object):
    def __init__(self, success=True, error=None):
        self.success = success
        self.error = error if error else ''
        self.totalCapacity = None
        self.availableCapacity = None
        self.poolCapacities = None


class PingResponse(AgentResponse):
    def __init__(self):
        super(PingResponse, self).__init__()
        self.uuid = None


class InitResponse(AgentResponse):
    def __init__(self):
        super(InitResponse, self).__init__()
        self.dhcpRangeBegin = None
        self.dhcpRangeEnd = None
        self.dhcpRangeNetmask = None


def reply_error(func):
    @functools.wraps(func)
    def wrap(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            content = traceback.format_exc()
            err = '%s\n%s\nargs:%s' % (str(e), content, pprint.pformat([args, kwargs]))
            rsp = AgentResponse()
            rsp.success = False
            rsp.error = str(e)
            logger.warn(err)
            return json_object.dumps(rsp)

    return wrap


class PxeServerAgent(object):
    ECHO_PATH = "/baremetal/pxeserver/echo"
    INIT_PATH = "/baremetal/pxeserver/init"
    PING_PATH = "/baremetal/pxeserver/ping"
    CONNECT_PATH = '/baremetal/pxeserver/connect'
    START_PATH = "/baremetal/pxeserver/start"
    STOP_PATH = "/baremetal/pxeserver/stop"
    CREATE_BM_CONFIGS_PATH = "/baremetal/pxeserver/createbmconfigs"
    DELETE_BM_CONFIGS_PATH = "/baremetal/pxeserver/deletebmconfigs"
    CREATE_BM_NGINX_PROXY_PATH = "/baremetal/pxeserver/createbmnginxproxy"
    DELETE_BM_NGINX_PROXY_PATH = "/baremetal/pxeserver/deletebmnginxproxy"
    DOWNLOAD_FROM_IMAGESTORE_PATH = "/baremetal/pxeserver/imagestore/download"
    DOWNLOAD_FROM_CEPHB_PATH = "/baremetal/pxeserver/cephb/download"
    DELETE_BM_IMAGE_CACHE_PATH = "/baremetal/pxeserver/deletecache"
    MOUNT_BM_IMAGE_CACHE_PATH = "/baremetal/pxeserver/mountcache"
    http_server = http.HttpServer(port=7770)
    http_server.logfile_path = log.get_logfile_path()

    BAREMETAL_LIB_PATH = "/var/lib/zstack/baremetal/"
    BAREMETAL_LOG_PATH = "/var/log/zstack/baremetal/"
    DNSMASQ_CONF_PATH = BAREMETAL_LIB_PATH + "dnsmasq/dnsmasq.conf"
    DHCP_HOSTS_FILE = BAREMETAL_LIB_PATH + "dnsmasq/hosts.dhcp"
    DNSMASQ_LOG_PATH = BAREMETAL_LOG_PATH + "dnsmasq.log"
    TFTPBOOT_PATH = BAREMETAL_LIB_PATH + "tftpboot/"
    VSFTPD_CONF_PATH = BAREMETAL_LIB_PATH + "vsftpd/vsftpd.conf"
    VSFTPD_ROOT_PATH = BAREMETAL_LIB_PATH + "ftp/"
    VSFTPD_LOG_PATH = BAREMETAL_LOG_PATH + "vsftpd.log"
    PXELINUX_CFG_PATH = TFTPBOOT_PATH + "pxelinux.cfg/"
    PXELINUX_DEFAULT_CFG = PXELINUX_CFG_PATH + "default"
    KS_CFG_PATH = VSFTPD_ROOT_PATH + "ks/"
    INSPECTOR_KS_CFG = KS_CFG_PATH + "inspector_ks.cfg"
    NGINX_CONF_PATH = "/etc/nginx/conf.d/pxe/"

    def __init__(self):
        self.uuid = None
        self.storage_path = None
        self.dhcp_interface = None

        self.http_server.register_sync_uri(self.ECHO_PATH, self.echo)
        self.http_server.register_sync_uri(self.CONNECT_PATH, self.connect)
        self.http_server.register_async_uri(self.INIT_PATH, self.init)
        self.http_server.register_async_uri(self.PING_PATH, self.ping)
        self.http_server.register_async_uri(self.START_PATH, self.start)
        self.http_server.register_async_uri(self.STOP_PATH, self.stop)
        self.http_server.register_async_uri(self.CREATE_BM_CONFIGS_PATH, self.create_bm_configs)
        self.http_server.register_async_uri(self.DELETE_BM_CONFIGS_PATH, self.delete_bm_configs)
        self.http_server.register_async_uri(self.CREATE_BM_NGINX_PROXY_PATH, self.create_bm_nginx_proxy)
        self.http_server.register_async_uri(self.DELETE_BM_NGINX_PROXY_PATH, self.delete_bm_nginx_proxy)
        self.http_server.register_async_uri(self.DOWNLOAD_FROM_IMAGESTORE_PATH, self.download_imagestore)
        self.http_server.register_async_uri(self.DOWNLOAD_FROM_CEPHB_PATH, self.download_cephb)
        self.http_server.register_async_uri(self.DELETE_BM_IMAGE_CACHE_PATH, self.delete_bm_image_cache)
        self.http_server.register_async_uri(self.MOUNT_BM_IMAGE_CACHE_PATH, self.mount_bm_image_cache)

        self.imagestore_client = ImageStoreClient()

    def _set_capacity_to_response(self, rsp):
        total, avail = self._get_capacity()
        rsp.totalCapacity = total
        rsp.availableCapacity = avail

    def _get_capacity(self):
        total = linux.get_total_disk_size(self.storage_path)
        used = linux.get_used_disk_size(self.storage_path)
        return total, total - used

    def _start_pxe_server(self):
        ret = bash_r("ps -ef | grep -v grep | grep dnsmasq || dnsmasq -C " + self.DNSMASQ_CONF_PATH)
        if ret != 0:
            logger.error("failed to start dnsmasq on baremetal pxeserver[uuid:%s]" % self.uuid)
            return ret
        ret = bash_r("ps -ef | grep -v grep | grep vsftpd || vsftpd " + self.VSFTPD_CONF_PATH)
        if ret != 0:
            logger.error("failed to start vsftpd on baremetal pxeserver[uuid:%s]" % self.uuid)
            return ret
        return 0

    def _stop_pxe_server(self):
        ret = bash_r("pkill -9 dnsmasq")
        if ret != 0:
            logger.error("failed to stop dnsmasq on baremetal pxeserver[uuid:%s]" % self.uuid)
            return ret
        ret = bash_r("pkill -9 vsftpd")
        if ret != 0:
            logger.error("failed to stop vsftpd on baremetal pxeserver[uuid:%s]" % self.uuid)
            return ret
        return 0

    @staticmethod
    def _get_ip_address(ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', ifname[:15])
        )[20:24])

    @staticmethod
    def _get_ip_netmask(ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x891b,  # SIOCGIFNETMASK
            struct.pack('256s', ifname[:15])
        )[20:24])

    @staticmethod
    def _get_mac_address(ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        info = fcntl.ioctl(s.fileno(), 0x8927, struct.pack('256s', ifname[:15]))
        return ':'.join(['%02x' % ord(char) for char in info[18:24]])

    def _get_dhcp_range(self, dhcp_interface, dhcp_netmask):
        dhcp_ip_address = self._get_ip_address(dhcp_interface)
        _range = list(IPNetwork("%s/%s" % (dhcp_ip_address, dhcp_netmask)))
        return _range[0], _range[-1]

    @reply_error
    def echo(self, req):
        logger.debug('get echoed')
        return ''

    @reply_error
    def init(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        self.uuid = cmd.uuid
        self.storage_path = cmd.storagePath

        # get pxe server capacity
        self._set_capacity_to_response(rsp)

        # init dhcp.conf
        dhcp_conf = """
interface={DHCP_INTERFACE}
port=0
dhcp-boot=pxelinux.0
enable-tftp
tftp-root={TFTPBOOT_PATH}
log-dhcp
log-facility={DNSMASQ_LOG_PATH}

dhcp-range={DHCP_RANGE}
dhcp-option=1,{DHCP_NETMASK}
dhcp-hostsfile={DHCP_HOSTS_FILE}
""".format(DHCP_INTERFACE=cmd.dhcpInterface,
           DHCP_RANGE="%s,%s,%s" % (cmd.dhcpRangeBegin, cmd.dhcpRangeEnd, cmd.dhcpRangeNetmask),
           DHCP_NETMASK=cmd.dhcpRangeNetmask,
           TFTPBOOT_PATH=self.TFTPBOOT_PATH,
           DHCP_HOSTS_FILE=self.DHCP_HOSTS_FILE,
           DNSMASQ_LOG_PATH=self.DNSMASQ_LOG_PATH)
        with open(self.DNSMASQ_CONF_PATH, 'w') as f:
            f.write(dhcp_conf)

        # init vsftpd.conf
        vsftpd_conf = """
anonymous_enable=YES
anon_root={VSFTPD_ANON_ROOT}
local_enable=YES
write_enable=YES
local_umask=022
dirmessage_enable=YES
connect_from_port_20=YES
listen=NO
listen_ipv6=YES
pam_service_name=vsftpd
userlist_enable=YES
tcp_wrappers=YES
xferlog_enable=YES
xferlog_std_format=YES
xferlog_file={VSFTPD_LOG_PATH}
""".format(VSFTPD_ANON_ROOT=self.VSFTPD_ROOT_PATH,
           VSFTPD_LOG_PATH=self.VSFTPD_LOG_PATH)
        with open(self.VSFTPD_CONF_PATH, 'w') as f:
            f.write(vsftpd_conf)
        os.chown(self.VSFTPD_CONF_PATH, 0, 0)

        # init pxelinux.cfg
        pxeserver_dhcp_nic_ip = self._get_ip_address(cmd.dhcpInterface)
        pxelinux_cfg = """
default zstack_baremetal
prompt 0

label zstack_baremetal
kernel zstack/vmlinuz
ipappend 2
append initrd=zstack/initrd.img devfs=nomount ksdevice=bootif ks=ftp://{PXESERVER_DHCP_NIC_IP}/ks/inspector_ks.cfg vnc
""".format(PXESERVER_DHCP_NIC_IP=pxeserver_dhcp_nic_ip)
        with open(self.PXELINUX_DEFAULT_CFG, 'w') as f:
            f.write(pxelinux_cfg)

        # init inspector_ks.cfg
        ks_tmpl_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'ks_tmpl')
        with open("%s/inspector_ks_tmpl" % ks_tmpl_path, 'r') as fr:
            inspector_ks_cfg = fr.read() \
                .replace("PXESERVERUUID", cmd.uuid) \
                .replace("PXESERVER_DHCP_NIC_IP", self._get_ip_address(cmd.dhcpInterface))
            with open(self.INSPECTOR_KS_CFG, 'w') as fw:
                fw.write(inspector_ks_cfg)

        # create nginx proxy for http://MN_IP:8080/zstack/asyncrest/sendcommand
        content = "location /zstack/asyncrest/sendcommand { proxy_pass http://%s:8080/zstack/asyncrest/sendcommand; }" % cmd.managementIp
        with open("/etc/nginx/conf.d/zstack_mn.conf", 'w') as fw:
            fw.write(content)

        # start pxe services
        if self._start_pxe_server() != 0:
            rsp.success = False
            rsp.error = "failed to start baremetal pxeserver[uuid:%s]" % self.uuid
        return json_object.dumps(rsp)

    @reply_error
    def ping(self, req):
        rsp = PingResponse()
        rsp.uuid = self.uuid
        return json_object.dumps(rsp)

    @reply_error
    def connect(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        self.uuid = cmd.uuid
        self.storage_path = cmd.storagePath
        if os.path.isfile(self.storage_path):
            raise Exception('storage path: %s is a file' % self.storage_path)
        if not os.path.exists(self.storage_path):
            os.makedirs(self.storage_path, 0777)

        total, avail = self._get_capacity()
        logger.debug(http.path_msg(self.CONNECT_PATH, 'connected, [storage path:%s, total capacity: %s bytes, '
                                                      'available capacity: %s size]' %
                                   (self.storage_path, total, avail)))
        rsp.totalCapacity = total
        rsp.availableCapacity = avail
        return json_object.dumps(rsp)

    @reply_error
    def start(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        self.uuid = cmd.uuid
        if self._start_pxe_server() != 0:
            rsp.success = False
            rsp.error = "failed to start baremetal pxeserver[uuid:%s]" % self.uuid
        return json_object.dumps(rsp)

    @reply_error
    def stop(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        self.uuid = cmd.uuid
        if self._stop_pxe_server() != 0:
            rsp.success = False
            rsp.error = "failed to stop baremetal pxeserver[uuid:%s]" % self.uuid
        return json_object.dumps(rsp)

    @reply_error
    def create_bm_configs(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        self.uuid = cmd.uuid
        self.dhcp_interface = cmd.dhcpInterface

        # create ks.cfg
        ks_cfg_file = self.KS_CFG_PATH + cmd.pxeNicMac.replace(":", "-")
        ks_tmpl_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), 'ks_tmpl')
        is_zstack_iso = os.path.exists(os.path.join(self.VSFTPD_ROOT_PATH, cmd.imageUuid, "Extra", "qemu-kvm-ev"))
        with open("%s/generic_ks_tmpl" % ks_tmpl_path, 'r') as fr:
            generic_ks_cfg = fr.read() \
                .replace("EXTRA_REPO", "" if is_zstack_iso else "repo --name=qemu-kvm-ev --baseurl=ftp://PXESERVER_DHCP_NIC_IP/zstack-dvd/Extra/qemu-kvm-ev") \
                .replace("PXESERVER_DHCP_NIC_IP", self._get_ip_address(cmd.dhcpInterface)) \
                .replace("BMUUID", cmd.bmUuid) \
                .replace("IMAGEUUID", cmd.imageUuid) \
                .replace("ROOT_PASSWORD", "rootpw --iscrypted " + cmd.customPassword) \
                .replace("NETWORK_SETTING", cmd.nicCfgs)
            with open(ks_cfg_file, 'w') as fw:
                fw.write(generic_ks_cfg)

        # create pxelinux.cfg
        pxeserver_dhcp_nic_ip = self._get_ip_address(cmd.dhcpInterface)
        pxe_cfg_file = self.PXELINUX_CFG_PATH + "01-" + cmd.pxeNicMac.replace(":", "-")
        pxelinux_cfg = """
default zstack_baremetal
prompt 0

label zstack_baremetal
kernel {IMAGEUUID}/vmlinuz
ipappend 2
append initrd={IMAGEUUID}/initrd.img devfs=nomount ksdevice=bootif ks=ftp://{PXESERVER_DHCP_NIC_IP}/ks/{KS_CFG_FILE} vnc
""".format(PXESERVER_DHCP_NIC_IP=pxeserver_dhcp_nic_ip,
           IMAGEUUID=cmd.imageUuid,
           KS_CFG_FILE=ks_cfg_file)
        with open(pxe_cfg_file, 'w') as f:
            f.write(pxelinux_cfg)

        return json_object.dumps(rsp)

    @reply_error
    def delete_bm_configs(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        pxe_cfg_file = self.PXELINUX_CFG_PATH + "01-" + cmd.pxeNicMac.replace(":", "-")
        if os.path.exists(pxe_cfg_file):
            os.remove(pxe_cfg_file)

        ks_cfg_file = self.KS_CFG_PATH + cmd.pxeNicMac.replace(":", "-")
        if os.path.exists(ks_cfg_file):
            os.remove(ks_cfg_file)

        return json_object.dumps(rsp)

    @reply_error
    def create_bm_nginx_proxy(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        nginx_proxy_file = self.NGINX_CONF_PATH + cmd.bmUuid
        with open(nginx_proxy_file, 'w') as f:
            f.write(cmd.upstream)

        return json_object.dumps(rsp)

    @reply_error
    def delete_bm_nginx_proxy(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        nginx_proxy_file = self.NGINX_CONF_PATH + cmd.bmUuid
        if os.path.exists(nginx_proxy_file):
            os.remove(nginx_proxy_file)

        return json_object.dumps(rsp)

    @reply_error
    def download_imagestore(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        # download
        rsp = self.imagestore_client.download_image_from_imagestore(cmd)
        if not rsp.success:
            self._set_capacity_to_response(rsp)
            return json_object.dumps(rsp)

        # mount
        cache_path = cmd.cacheInstallPath
        mount_path = os.path.join(self.VSFTPD_ROOT_PATH, cmd.imageUuid)
        os.makedirs(mount_path)
        ret = bash_r("sudo mount %s %s" % (cache_path, mount_path))
        if ret != 0:
            rsp.success = False
            rsp.error = "failed to mount image[uuid:%s] to baremetal cache %s" % (cmd.imageUuid, cache_path)
            self._set_capacity_to_response(rsp)
            return json_object.dumps(rsp)

        # copy vmlinuz etc.
        vmlinuz_path = os.path.join(self.TFTPBOOT_PATH, cmd.imageUuid)
        os.makedirs(vmlinuz_path)
        shutil.copyfile(os.path.join(mount_path, "isolinux/vmlinuz*"), os.path.join(vmlinuz_path, "vmlinuz"))
        shutil.copyfile(os.path.join(mount_path, "isolinux/initrd*.img"), os.path.join(vmlinuz_path, "initrd.img"))

        self._set_capacity_to_response(rsp)
        return json_object.dumps(rsp)

    @reply_error
    def download_cephb(self, req):
        # TODO
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        return json_object.dumps(rsp)

    @reply_error
    def delete_bm_image_cache(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        # rm vmlinuz etc.
        vmlinuz_path = os.path.join(self.TFTPBOOT_PATH, cmd.imageUuid)
        os.rmdir(vmlinuz_path)

        # umount
        mount_path = os.path.join(self.VSFTPD_ROOT_PATH, cmd.imageUuid)
        bash_r("sudo umount %s" % mount_path)
        os.rmdir(mount_path)

        # rm image cache
        os.rmdir(os.path.pardir(cmd.cacheInstallPath))

        self._set_capacity_to_response(rsp)
        return json_object.dumps(rsp)

    @reply_error
    def mount_bm_image_cache(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        cache_path = cmd.cacheInstallPath
        mount_path = os.path.join(self.VSFTPD_ROOT_PATH, cmd.imageUuid)
        ret = bash_r("mount | grep %s || sudo mount %s %s" % (mount_path, cache_path, mount_path))
        if ret != 0:
            rsp.success = False
            rsp.error = "failed to mount baremetal cache of image[uuid:%s]" % cmd.imageUuid

        return json_object.dumps(rsp)


class PxeServerDaemon(daemon.Daemon):
    def __init__(self, pidfile):
        super(PxeServerDaemon, self).__init__(pidfile)
        self.agent = PxeServerAgent()

    def run(self):
        self.agent.http_server.start()
