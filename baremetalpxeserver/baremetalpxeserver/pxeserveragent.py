__author__ = 'frank'

import os
import os.path
import pprint
import shutil
import socket
import fcntl
import struct
import hashlib
import traceback
from jinja2 import Template
from netaddr import IPNetwork, IPAddress

import zstacklib.utils.daemon as daemon
import zstacklib.utils.http as http
import zstacklib.utils.jsonobject as json_object
from zstacklib.utils.bash import *
from imagestore import ImageStoreClient

logger = log.get_logger(__name__)

class PxeServerError(Exception):
    '''baremetal pxeserver error'''

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
    AGENT_PORT = 7770
    NGINX_MN_PROXY_PORT = 7771
    NGINX_TERMINAL_PROXY_PORT = 7772
    WEBSOCKIFY_PORT = 6080

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
    CREATE_BM_NOVNC_PROXY_PATH = "/baremetal/pxeserver/createbmnovncproxy"
    DELETE_BM_NOVNC_PROXY_PATH = "/baremetal/pxeserver/deletebmnovncproxy"
    DOWNLOAD_FROM_IMAGESTORE_PATH = "/baremetal/pxeserver/imagestore/download"
    DOWNLOAD_FROM_CEPHB_PATH = "/baremetal/pxeserver/cephb/download"
    DELETE_BM_IMAGE_CACHE_PATH = "/baremetal/pxeserver/deletecache"
    MOUNT_BM_IMAGE_CACHE_PATH = "/baremetal/pxeserver/mountcache"
    http_server = http.HttpServer(port=AGENT_PORT)
    http_server.logfile_path = log.get_logfile_path()

    BAREMETAL_LIB_PATH = "/var/lib/zstack/baremetal/"
    BAREMETAL_LOG_PATH = "/var/log/zstack/baremetal/"
    DNSMASQ_CONF_PATH = BAREMETAL_LIB_PATH + "dnsmasq/dnsmasq.conf"
    HOSTS_DHCP_FILE = BAREMETAL_LIB_PATH + "dnsmasq/hosts.dhcp"
    DNSMASQ_LOG_PATH = BAREMETAL_LOG_PATH + "dnsmasq.log"
    TFTPBOOT_PATH = BAREMETAL_LIB_PATH + "tftpboot/"
    VSFTPD_CONF_PATH = BAREMETAL_LIB_PATH + "vsftpd/vsftpd.conf"
    VSFTPD_ROOT_PATH = BAREMETAL_LIB_PATH + "ftp/"
    VSFTPD_LOG_PATH = BAREMETAL_LOG_PATH + "vsftpd.log"
    PXELINUX_CFG_PATH = TFTPBOOT_PATH + "pxelinux.cfg/"
    PXELINUX_DEFAULT_CFG = PXELINUX_CFG_PATH + "default"
    # we use `KS_CFG_PATH` to hold kickstart/preseed/autoyast preconfiguration files
    KS_CFG_PATH = VSFTPD_ROOT_PATH + "ks/"
    INSPECTOR_KS_CFG = KS_CFG_PATH + "inspector_ks.cfg"
    ZSTACK_SCRIPTS_PATH = VSFTPD_ROOT_PATH + "scripts/"
    NGINX_MN_PROXY_CONF_PATH = "/etc/nginx/conf.d/pxe_mn/"
    NGINX_TERMINAL_PROXY_CONF_PATH = "/etc/nginx/conf.d/terminal/"
    NOVNC_INSTALL_PATH = BAREMETAL_LIB_PATH + "noVNC/"
    NOVNC_TOKEN_PATH = NOVNC_INSTALL_PATH + "tokens/"

    NMAP_BROADCAST_DHCP_DISCOVER_PATH = "/usr/share/nmap/scripts/broadcast-dhcp-discover.nse"

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
        self.http_server.register_async_uri(self.CREATE_BM_NOVNC_PROXY_PATH, self.create_bm_novnc_proxy)
        self.http_server.register_async_uri(self.DELETE_BM_NOVNC_PROXY_PATH, self.delete_bm_novnc_proxy)
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
        ret = bash_r("ps -ef | grep -v 'grep' | grep 'dnsmasq -C {0}' || dnsmasq -C {0} -u root".format(self.DNSMASQ_CONF_PATH))
        if ret != 0:
            raise PxeServerError("failed to start dnsmasq on baremetal pxeserver[uuid:%s]" % self.uuid)

        ret = bash_r("ps -ef | grep -v 'grep' | grep 'vsftpd {0}' || vsftpd {0}".format(self.VSFTPD_CONF_PATH))
        if ret != 0:
            raise PxeServerError("failed to start vsftpd on baremetal pxeserver[uuid:%s]" % self.uuid)

        ret = bash_r("ps -ef | grep -v 'grep' | grep 'websockify' | grep 'baremetal' || "
                     "python %s/utils/websockify/run --web %s --token-plugin TokenFile --token-source=%s -D 6080"
                     % (self.NOVNC_INSTALL_PATH, self.NOVNC_INSTALL_PATH, self.NOVNC_TOKEN_PATH))
        if ret != 0:
            raise PxeServerError("failed to start noVNC on baremetal pxeserver[uuid:%s]" % self.uuid)

        # in case nginx config is updated during nginx running
        ret = bash_r("systemctl start nginx && systemctl reload nginx")
        if ret != 0:
            raise PxeServerError("failed to start nginx on baremetal pxeserver[uuid:%s]" % self.uuid)

    # we do not stop nginx on pxeserver because it may be needed by bm with terminal proxy
    # stop pxeserver means stop dnsmasq actually
    def _stop_pxe_server(self):
        bash_r("kill -9 `ps -ef | grep -v grep | grep 'vsftpd %s' | awk '{ print $2 }'`" % self.VSFTPD_CONF_PATH)
        bash_r("kill -9 `ps -ef | grep -v grep | grep websockify | grep baremetal | awk '{ print $2 }'`")
        bash_r("kill -9 `ps -ef | grep -v grep | grep 'dnsmasq -C %s' | awk '{ print $2 }'`" % self.DNSMASQ_CONF_PATH)

    @staticmethod
    def _get_mac_address(ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        info = fcntl.ioctl(s.fileno(), 0x8927,  struct.pack('256s', ifname[:15]))
        return ':'.join(['%02x' % ord(char) for char in info[18:24]])

    @staticmethod
    def _get_ip_address(ifname):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        return socket.inet_ntoa(fcntl.ioctl(
            s.fileno(),
            0x8915,  # SIOCGIFADDR
            struct.pack('256s', ifname[:15])
        )[20:24])

    @staticmethod
    def _is_belong_to_same_subnet(addr1, addr2, netmask):
        return IPAddress(addr1) in IPNetwork("%s/%s" % (addr2, netmask))

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

        # check dhcp interface and dhcp range
        pxeserver_dhcp_nic_ip = self._get_ip_address(cmd.dhcpInterface).strip()
        pxeserver_dhcp_nic_nm = linux.get_netmask_of_nic(cmd.dhcpInterface).strip()
        if not self._is_belong_to_same_subnet(cmd.dhcpRangeBegin, pxeserver_dhcp_nic_ip, pxeserver_dhcp_nic_nm) or \
                not self._is_belong_to_same_subnet(cmd.dhcpRangeEnd, pxeserver_dhcp_nic_ip, pxeserver_dhcp_nic_nm):
            raise PxeServerError("%s ~ %s cannot connect to dhcp interface %s" % (cmd.dhcpRangeBegin, cmd.dhcpRangeEnd, cmd.dhcpInterface))

        # get pxe server capacity
        self._set_capacity_to_response(rsp)

        # clean up old configs
        if os.path.exists(self.PXELINUX_CFG_PATH):
            bash_r("rm -f %s/*" % self.PXELINUX_CFG_PATH)
        if os.path.exists(self.KS_CFG_PATH):
            bash_r("rm -f %s/*" % self.KS_CFG_PATH)
        if os.path.exists(self.NGINX_MN_PROXY_CONF_PATH):
            bash_r("rm -f %s/*" % self.NGINX_MN_PROXY_CONF_PATH)
        if os.path.exists(self.NGINX_TERMINAL_PROXY_CONF_PATH):
            bash_r("rm -f %s/*" % self.NGINX_TERMINAL_PROXY_CONF_PATH)
        if os.path.exists(self.NOVNC_TOKEN_PATH):
            bash_r("rm -f %s/*" % self.NOVNC_TOKEN_PATH)

        # init dnsmasq.conf
        dhcp_conf = """interface={DHCP_INTERFACE}
port=0
dhcp-boot=pxelinux.0
enable-tftp
tftp-root={TFTPBOOT_PATH}
log-facility={DNSMASQ_LOG_PATH}
dhcp-range={DHCP_RANGE}
dhcp-option=1,{DHCP_NETMASK}
dhcp-hostsfile={HOSTS_DHCP_FILE}
""".format(DHCP_INTERFACE=cmd.dhcpInterface,
           DHCP_RANGE="%s,%s,%s" % (cmd.dhcpRangeBegin, cmd.dhcpRangeEnd, cmd.dhcpRangeNetmask),
           DHCP_NETMASK=cmd.dhcpRangeNetmask,
           TFTPBOOT_PATH=self.TFTPBOOT_PATH,
           HOSTS_DHCP_FILE=self.HOSTS_DHCP_FILE,
           DNSMASQ_LOG_PATH=self.DNSMASQ_LOG_PATH)
        with open(self.DNSMASQ_CONF_PATH, 'w') as f:
            f.write(dhcp_conf)

        # init hosts.dhcp
        mac_address = self._get_mac_address(cmd.dhcpInterface)
        dhcp_conf = "%s,ignore" % mac_address
        with open(self.HOSTS_DHCP_FILE, 'w') as f:
            f.write(dhcp_conf)

        # hack nmap script
        splited_mac_address = "0x" + mac_address.replace(":", ",0x")
        bash_r("sed -i '/local mac = string.char/s/0x..,0x..,0x..,0x..,0x..,0x../%s/g' %s" % \
                (splited_mac_address, self.NMAP_BROADCAST_DHCP_DISCOVER_PATH))

        # init vsftpd.conf
        vsftpd_conf = """anonymous_enable=YES
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

        # init pxelinux.cfg
        pxelinux_cfg = """default zstack_baremetal
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
                .replace("PXESERVER_DHCP_NIC_IP", pxeserver_dhcp_nic_ip)
            with open(self.INSPECTOR_KS_CFG, 'w') as fw:
                fw.write(inspector_ks_cfg)

        # config nginx
        if not os.path.exists(self.NGINX_MN_PROXY_CONF_PATH):
            os.makedirs(self.NGINX_MN_PROXY_CONF_PATH, 0777)
        if not os.path.exists(self.NGINX_TERMINAL_PROXY_CONF_PATH):
            os.makedirs(self.NGINX_TERMINAL_PROXY_CONF_PATH, 0777)
        nginx_conf = """user nginx;
worker_processes auto;
error_log /var/log/nginx/error.log;
pid /run/nginx.pid;
include /usr/share/nginx/modules/*.conf;
events {
    worker_connections 1024;
}
http {
    access_log          /var/log/nginx/access.log;
    sendfile            on;
    tcp_nopush          on;
    tcp_nodelay         on;
    keepalive_timeout   1000;
    types_hash_max_size 2048;
    include             /etc/nginx/mime.types;
    default_type        application/octet-stream;

    map $http_upgrade $connection_upgrade {
        default upgrade;
        ''      close;
    }

    server {
        listen 8090;
        include /etc/nginx/conf.d/mn_pxe/*;
    }

    server {
        listen 7771;
        include /etc/nginx/conf.d/pxe_mn/*;
    }

    server {
        listen 7772;
        include /etc/nginx/conf.d/terminal/*;
    }
}
"""
        with open("/etc/nginx/nginx.conf", 'w') as fw:
            fw.write(nginx_conf)

        # create nginx proxy for http://MN_IP:8080/zstack/asyncrest/sendcommand
        content = "location / { proxy_pass http://%s:8080/; }" % cmd.managementIp
        with open("/etc/nginx/conf.d/pxe_mn/zstack_mn.conf", 'w') as fw:
            fw.write(content)

        # install noVNC
        if not os.path.exists(self.NOVNC_INSTALL_PATH):
            ret = bash_r("tar -xf %s -C %s" % (os.path.join(self.BAREMETAL_LIB_PATH, "noVNC.tar.gz"), self.BAREMETAL_LIB_PATH))
            if ret != 0:
                raise PxeServerError("failed to install noVNC on baremetal pxeserver[uuid:%s]" % self.uuid)

        # restart pxe services
        self._stop_pxe_server()
        self._start_pxe_server()

        logger.info("successfully inited and started baremetal pxeserver[uuid:%s]" % self.uuid)
        return json_object.dumps(rsp)

    @reply_error
    def ping(self, req):
        rsp = PingResponse()
        rsp.uuid = self.uuid

        # DETECT ROGUE DHCP SERVER
        cmd = json_object.loads(req[http.REQUEST_BODY])
        ret, output = bash_ro("nmap -sU -p67 --script broadcast-dhcp-discover -e %s | grep 'Server Identifier'" % cmd.dhcpInterface)
        if ret == 0:
            raise PxeServerError("rogue dhcp server[IP:%s] detected" % output.strip().split(' ')[-1])

        # make sure pxeserver is running if it's Enabled
        if cmd.enabled:
            self._start_pxe_server()

        return json_object.dumps(rsp)

    @reply_error
    def connect(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        self.uuid = cmd.uuid
        self.storage_path = cmd.storagePath

        # check storage path
        if os.path.isfile(self.storage_path):
            raise PxeServerError('storage path: %s is a file' % self.storage_path)

        if not os.path.exists(self.storage_path):
            os.makedirs(self.storage_path, 0777)

        total, avail = self._get_capacity()
        logger.debug(http.path_msg(self.CONNECT_PATH, 'connected, [storage path:%s, total capacity: %s bytes, '
                                                      'available capacity: %s size]' %
                                   (self.storage_path, total, avail)))
        rsp.totalCapacity = total
        rsp.availableCapacity = avail
        return json_object.dumps(rsp)

    @in_bash
    @reply_error
    def start(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        self.uuid = cmd.uuid
        self._start_pxe_server()

        logger.info("successfully started baremetal pxeserver[uuid:%s]")
        return json_object.dumps(rsp)

    @in_bash
    @reply_error
    def stop(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        self.uuid = cmd.uuid
        self._stop_pxe_server()

        logger.info("successfully stopped baremetal pxeserver[uuid:%s]" % self.uuid)
        return json_object.dumps(rsp)

    @reply_error
    def create_bm_configs(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        # check preconfiguration md5sum
        if hashlib.md5(cmd.preconfigurationContent).hexdigest() != cmd.preconfigurationMd5sum:
            raise PxeServerError("preconfiguration content not complete")

        self.uuid = cmd.uuid
        self.dhcp_interface = cmd.dhcpInterface
        self._create_pxelinux_cfg(cmd)
        self._create_preconfiguration_file(cmd)
        logger.info("successfully created pxelinux.cfg and preconfiguration file for baremetal instance[uuid:%s] on pxeserver[uuid:%s]" % (cmd.bmUuid, self.uuid))
        return json_object.dumps(rsp)

    def _create_pxelinux_cfg(self, cmd):
        ks_cfg_name = cmd.pxeNicMac.replace(":", "-")
        pxe_cfg_file = os.path.join(self.PXELINUX_CFG_PATH, "01-" + ks_cfg_name)
        pxeserver_dhcp_nic_ip = self._get_ip_address(cmd.dhcpInterface).strip()

        append = ""
        if cmd.preconfigurationType == 'kickstart':
            append = 'devfs=nomount ksdevice=bootif ks=ftp://{PXESERVER_DHCP_NIC_IP}/ks/{KS_CFG_NAME} vnc'
        elif cmd.preconfigurationType == 'preseed':
            append = 'auto=true priority=critical url=ftp://{PXESERVER_DHCP_NIC_IP}/ks/{KS_CFG_NAME}'
        elif cmd.preconfigurationType == 'autoyast':
            append = 'install=ftp://{PXESERVER_DHCP_NIC_IP}/{IMAGEUUID}/ autoyast=ftp://{PXESERVER_DHCP_NIC_IP}/ks/{KS_CFG_NAME} vnc'
        append = append.format(PXESERVER_DHCP_NIC_IP=pxeserver_dhcp_nic_ip,
                IMAGEUUID=cmd.imageUuid,
                KS_CFG_NAME=ks_cfg_name)

        pxelinux_cfg = ("default {IMAGEUUID}\n"
                        "prompt 0\n"
                        "ipappend 2\n"
                        "label {IMAGEUUID}\n"
                        "kernel {IMAGEUUID}/vmlinuz\n"
                        "append initrd={IMAGEUUID}/initrd.img {APPEND}").format(
            PXESERVER_DHCP_NIC_IP=pxeserver_dhcp_nic_ip,
            IMAGEUUID=cmd.imageUuid,
            KS_CFG_NAME=ks_cfg_name,
            APPEND=append)

        with open(pxe_cfg_file, 'w') as f:
            f.write(pxelinux_cfg)

    def _create_preconfiguration_file(self, cmd):
        # in case user didn't seleted a preconfiguration template etc.
        cmd.preconfigurationContent = cmd.preconfigurationContent if cmd.preconfigurationContent != "" else """
        {{ extra_repo }}
        {{ REPO_URL }}
        {{ SYS_USERNAME }}
        {{ SYS_PASSWORD }}
        {{ NETWORK_CFGS }}
        {{ FORCE_INSTALL }}
        {{ PRE_SCRIPTS }}
        {{ POST_SCRIPTS }}
        """

        pxeserver_dhcp_nic_ip = self._get_ip_address(cmd.dhcpInterface).strip()
        if cmd.preconfigurationType == 'kickstart':
            rendered_content = self._render_kickstart_template(cmd, pxeserver_dhcp_nic_ip)
        elif cmd.preconfigurationType == 'preseed':
            rendered_content = self._render_preseed_template(cmd, pxeserver_dhcp_nic_ip)
        elif cmd.preconfigurationType == 'autoyast':
            rendered_content = self._render_autoyast_template(cmd, pxeserver_dhcp_nic_ip)
        else:
            raise PxeServerError("unkown preconfiguration type %s" % cmd.preconfigurationType)

        ks_cfg_name = cmd.pxeNicMac.replace(":", "-")
        ks_cfg_file = os.path.join(self.KS_CFG_PATH, ks_cfg_name)
        with open(ks_cfg_file, 'w') as f:
            f.write(rendered_content)

    def _create_pre_scripts(self, cmd, pxeserver_dhcp_nic_ip, more_script = ""):
        # poweroff and abort the provisioning process if failed to send `deploybegin` command
        pre_script = """# notify deploy begin
curl --fail -X POST -H "Content-Type:application/json" \
-H "commandpath:/baremetal/instance/deploybegin" \
-d {{"baremetalInstanceUuid":"{BMUUID}"}} \
--retry 3 \
http://{PXESERVER_DHCP_NIC_IP}:7771/zstack/asyncrest/sendcommand || poweroff
""".format(BMUUID=cmd.bmUuid, PXESERVER_DHCP_NIC_IP=pxeserver_dhcp_nic_ip)

        pre_script += more_script
        with open(os.path.join(self.ZSTACK_SCRIPTS_PATH, "pre_%s.sh" % cmd.bmUuid), 'w') as f:
            f.write(pre_script)

    def _create_post_scripts(self, cmd, pxeserver_dhcp_nic_ip, more_script = ""):
        post_script = """# notify deploy complete
curl -X POST -H "Content-Type:application/json" \
-H "commandpath:/baremetal/instance/deploycomplete" \
-d {{"baremetalInstanceUuid":"{BMUUID}"}} \
--retry 5 \
http://{PXESERVER_DHCP_NIC_IP}:7771/zstack/asyncrest/sendcommand

# baby agent
wget -P /usr/bin ftp://{PXESERVER_DHCP_NIC_IP}/shellinaboxd || curl -o /usr/bin/shellinaboxd ftp://{PXESERVER_DHCP_NIC_IP}/shellinaboxd
chmod a+x /usr/bin/shellinaboxd

if [ ! -f /etc/rc.local ]; then
cat > /etc/rc.local << EOF
#!/bin/bash
EOF
chmod a+x /etc/rc.local
fi
[ -f /etc/rc.d/rc.local ] && chmod a+x /etc/rc.d/rc.local
cat >> /etc/rc.local << EOF
iptables-save | grep -- "-I INPUT -p tcp -m tcp --dport 4200 -j ACCEPT" > /dev/null || (iptables -I INPUT -p tcp -m tcp --dport 4200 -j ACCEPT && service iptables save)
firewall-cmd --query-port=4200/tcp || (firewall-cmd --zone=public --add-port=4200/tcp --permanent && service firewalld restart)
shellinaboxd -b -t -s /:SSH:127.0.0.1
curl -X POST -H "Content-Type:application/json" \
-H "commandpath:/baremetal/instance/osrunning" \
-d {{"baremetalInstanceUuid":"{BMUUID}"}} \
--retry 5 \
http://{PXESERVER_DHCP_NIC_IP}:7771/zstack/asyncrest/sendcommand
EOF
""".format(BMUUID=cmd.bmUuid, PXESERVER_DHCP_NIC_IP=pxeserver_dhcp_nic_ip)

        post_script += more_script
        with open(os.path.join(self.ZSTACK_SCRIPTS_PATH, "post_%s.sh" % cmd.bmUuid), 'w') as f:
            f.write(post_script)

    def _render_kickstart_template(self, cmd, pxeserver_dhcp_nic_ip):
        is_zstack_iso = os.path.exists(os.path.join(self.VSFTPD_ROOT_PATH, cmd.imageUuid, "Extra", "qemu-kvm-ev"))
        self._create_pre_scripts(cmd, pxeserver_dhcp_nic_ip)
        self._create_post_scripts(cmd, pxeserver_dhcp_nic_ip)

        context = dict()
        context['REPO_URL'] = "ftp://%s/%s/" % (pxeserver_dhcp_nic_ip, cmd.imageUuid)
        context['USERNAME'] = cmd.username
        context['PASSWORD'] = cmd.password
        context['PRE_SCRIPTS'] = 'sh -c "$(curl -fsSL ftp://%s/scripts/pre_%s.sh)"' % (pxeserver_dhcp_nic_ip, cmd.bmUuid)
        context['POST_SCRIPTS'] = 'sh -c "$(curl -fsSL ftp://%s/scripts/post_%s.sh)"' % (pxeserver_dhcp_nic_ip, cmd.bmUuid)
        context['FORCE_INSTALL'] = "clearpart --all --initlabel" if cmd.forceInstall else ""

        niccfgs = json_object.loads(cmd.nicCfgs) if cmd.nicCfgs is not None else []
        nic_cfg_content = """
{% for cfg in niccfgs %}
network --bootproto=static --onboot=yes --noipv6
{%- if cfg.pxe -%}
{{PH}} --activate
{%- else -%}
{{PH}} --nodefroute
{%- endif -%}

{%- if cfg.bondName -%}
{{PH}} --device {{ cfg.bondName }} --bondslaves {{ cfg.bondSlaves }} --bondopts="mode={{ cfg.bondMode }} {{ cfg.bondOpts }}"
{%- else -%}
{{PH}} --device {{ cfg.mac }}
{%- endif -%}

{{PH}} --ip={{ cfg.ip }} --netmask={{ cfg.netmask }} --gateway={{ cfg.gateway }} --nameserver={{ cfg.nameserver }}

{%- if cfg.vlanid -%}
{{PH}} --vlanid={{ cfg.vlanid }}
{%- endif -%}
{% endfor %}
"""
        nic_cfg_tmpl = Template(nic_cfg_content)
        context['NETWORK_CFGS'] = nic_cfg_tmpl.render(niccfgs=niccfgs)

        if is_zstack_iso:
            context['extra_repo'] = "repo --name=qemu-kvm-ev --baseurl=ftp://%s/%s/Extra/qemu-kvm-ev" % (pxeserver_dhcp_nic_ip, cmd.imageUuid)
            context['pxeserver_dhcp_nic_ip'] = pxeserver_dhcp_nic_ip

        custom = json_object.loads(cmd.customPreconfigurations) if cmd.customPreconfigurations is not None else {}
        context.update(custom)

        tmpl = Template(cmd.preconfigurationContent)
        return tmpl.render(context)

    def _render_preseed_template(self, cmd, pxeserver_dhcp_nic_ip):
        niccfgs = json_object.loads(cmd.nicCfgs) if cmd.nicCfgs is not None else []

        self._create_pre_scripts(cmd, pxeserver_dhcp_nic_ip)
        self._create_post_scripts(cmd, pxeserver_dhcp_nic_ip)

        context = dict()
        context['REPO_URL'] = ("d-i mirror/protocol string ftp"
                               "d-i mirror/ftp/hostname string {PXESERVER_DHCP_NIC_IP}" 
                               "d-i mirror/ftp/directory string /{IMAGEUUID}")\
            .format(PXESERVER_DHCP_NIC_IP=pxeserver_dhcp_nic_ip, IMAGEUUID=cmd.imageUuid)
        context['USERNAME'] = cmd.username
        context['PASSWORD'] = cmd.password
        context['PRE_SCRIPTS'] = 'sh -c "$(curl -fsSL ftp://%s/scripts/pre_%s.sh)"' % (pxeserver_dhcp_nic_ip, cmd.bmUuid)
        context['POST_SCRIPTS'] = 'sh -c "$(curl -fsSL ftp://%s/scripts/post_%s.sh)"' % (pxeserver_dhcp_nic_ip, cmd.bmUuid)

        custom = json_object.loads(cmd.customPreconfigurations) if cmd.customPreconfigurations is not None else {}
        context.update(custom)

        tmpl = Template(cmd.preconfigurationContent)
        return tmpl.render(context)

    def _render_autoyast_template(self, cmd, pxeserver_dhcp_nic_ip):
        niccfgs = json_object.loads(cmd.nicCfgs) if cmd.nicCfgs is not None else []

        self._create_pre_scripts(cmd, pxeserver_dhcp_nic_ip)
        self._create_post_scripts(cmd, pxeserver_dhcp_nic_ip)

        context = dict()
        context['USERNAME'] = cmd.username
        context['PASSWORD'] = cmd.password
        context['PRE_SCRIPTS'] = 'sh -c "$(curl -fsSL ftp://%s/scripts/pre_%s.sh)"' % (pxeserver_dhcp_nic_ip, cmd.bmUuid)
        context['POST_SCRIPTS'] = 'sh -c "$(curl -fsSL ftp://%s/scripts/post_%s.sh)"' % (pxeserver_dhcp_nic_ip, cmd.bmUuid)

        custom = json_object.loads(cmd.customPreconfigurations) if cmd.customPreconfigurations is not None else {}
        context.update(custom)

        tmpl = Template(cmd.preconfigurationContent)
        return tmpl.render(context)

    @reply_error
    def delete_bm_configs(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        # clean up pxeserver bm configs
        if cmd.pxeNicMac == "*":
            if os.path.exists(self.PXELINUX_CFG_PATH):
                bash_r("rm -f %s/*" % self.PXELINUX_CFG_PATH)
            if os.path.exists(self.KS_CFG_PATH):
                bash_r("rm -f %s/*" % self.KS_CFG_PATH)
            if os.path.exists(self.NGINX_MN_PROXY_CONF_PATH):
                bash_r("rm -f %s/*" % self.NGINX_MN_PROXY_CONF_PATH)
            if os.path.exists(self.NGINX_TERMINAL_PROXY_CONF_PATH):
                bash_r("rm -f %s/*" % self.NGINX_TERMINAL_PROXY_CONF_PATH)
            if os.path.exists(self.NOVNC_TOKEN_PATH):
                bash_r("rm -f %s/*" % self.NOVNC_TOKEN_PATH)
        else:
            pxe_cfg_file = os.path.join(self.PXELINUX_CFG_PATH, "01-" + cmd.pxeNicMac.replace(":", "-"))
            if os.path.exists(pxe_cfg_file):
                os.remove(pxe_cfg_file)

            ks_cfg_file = os.path.join(self.KS_CFG_PATH, cmd.pxeNicMac.replace(":", "-"))
            if os.path.exists(ks_cfg_file):
                os.remove(ks_cfg_file)

        logger.info("successfully deleted pxelinux.cfg and ks.cfg %s" % cmd.pxeNicMac if cmd.pxeNicMac != '*' else 'all')
        return json_object.dumps(rsp)

    @reply_error
    def create_bm_nginx_proxy(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        nginx_proxy_file = os.path.join(self.NGINX_TERMINAL_PROXY_CONF_PATH, cmd.bmUuid)
        with open(nginx_proxy_file, 'w') as f:
            f.write(cmd.upstream)
        bash_r("systemctl reload nginx")

        logger.info("successfully create terminal nginx proxy for baremetal instance[uuid:%s] on pxeserver[uuid:%s]" % (cmd.bmUuid, self.uuid))
        return json_object.dumps(rsp)

    @reply_error
    def delete_bm_nginx_proxy(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        nginx_proxy_file = os.path.join(self.NGINX_TERMINAL_PROXY_CONF_PATH, cmd.bmUuid)
        if os.path.exists(nginx_proxy_file):
            os.remove(nginx_proxy_file)
        bash_r("systemctl reload nginx")

        logger.info("successfully deleted terminal nginx proxy for baremetal instance[uuid:%s] on pxeserver[uuid:%s]" % (cmd.bmUuid, self.uuid))
        return json_object.dumps(rsp)

    @reply_error
    def create_bm_novnc_proxy(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        novnc_proxy_file = os.path.join(self.NOVNC_TOKEN_PATH, cmd.bmUuid)
        with open(novnc_proxy_file, 'w') as f:
            f.write(cmd.upstream)

        logger.info("successfully created novnc proxy for baremetal instance[uuid:%s] on pxeserver[uuid:%s]" % (cmd.bmUuid, self.uuid))
        return json_object.dumps(rsp)

    @reply_error
    def delete_bm_novnc_proxy(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        novnc_proxy_file = os.path.join(self.NOVNC_TOKEN_PATH, cmd.bmUuid)
        if os.path.exists(novnc_proxy_file):
            os.remove(novnc_proxy_file)

        logger.info("successfully deleted novnc proxy for baremetal instance[uuid:%s] on pxeserver[uuid:%s]" % (cmd.bmUuid, self.uuid))
        return json_object.dumps(rsp)

    @in_bash
    @reply_error
    def download_imagestore(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        # download
        rsp = self.imagestore_client.download_image_from_imagestore(cmd)
        if not rsp.success:
            raise PxeServerError("failed to download image[uuid:%s] from imagestore to baremetal image cache" % cmd.imageUuid)

        # mount
        cache_path = cmd.cacheInstallPath
        mount_path = os.path.join(self.VSFTPD_ROOT_PATH, cmd.imageUuid)
        if not os.path.exists(mount_path):
            os.makedirs(mount_path)
        ret = bash_r("mount | grep %s || mount %s %s" % (mount_path, cache_path, mount_path))
        if ret != 0:
            raise PxeServerError("failed to mount image[uuid:%s] to baremetal ftp server %s" % (cmd.imageUuid, cache_path))

        # copy vmlinuz etc.
        vmlinuz_path = os.path.join(self.TFTPBOOT_PATH, cmd.imageUuid)
        if not os.path.exists(vmlinuz_path):
            os.makedirs(vmlinuz_path)
        ret1 = bash_r("cp %s %s" % (os.path.join(mount_path, "isolinux/vmlinuz*"), os.path.join(vmlinuz_path, "vmlinuz")))
        ret2 = bash_r("cp %s %s" % (os.path.join(mount_path, "isolinux/initrd*.img"), os.path.join(vmlinuz_path, "initrd.img")))
        if ret1 != 0 or ret2 != 0:
            raise PxeServerError("failed to copy vmlinuz and initrd.img from image[uuid:%s] to baremetal tftp server" % cmd.imageUuid)

        logger.info("successfully downloaded image[uuid:%s] and mounted it" % cmd.imageUuid)
        self._set_capacity_to_response(rsp)
        return json_object.dumps(rsp)

    @reply_error
    def download_cephb(self, req):
        # TODO
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()
        return json_object.dumps(rsp)

    @in_bash
    @reply_error
    def delete_bm_image_cache(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        # rm vmlinuz etc.
        vmlinuz_path = os.path.join(self.TFTPBOOT_PATH, cmd.imageUuid)
        if os.path.exists(vmlinuz_path):
            shutil.rmtree(vmlinuz_path)

        # umount
        mount_path = os.path.join(self.VSFTPD_ROOT_PATH, cmd.imageUuid)
        bash_r("umount {0}; rm -rf {0}".format(mount_path))

        # rm image cache
        if os.path.exists(cmd.cacheInstallPath):
            shutil.rmtree(os.path.dirname(cmd.cacheInstallPath))

        logger.info("successfully umounted and deleted cache of image[uuid:%s]" % cmd.imageUuid)
        self._set_capacity_to_response(rsp)
        return json_object.dumps(rsp)

    @in_bash
    @reply_error
    def mount_bm_image_cache(self, req):
        cmd = json_object.loads(req[http.REQUEST_BODY])
        rsp = AgentResponse()

        cache_path = cmd.cacheInstallPath
        mount_path = os.path.join(self.VSFTPD_ROOT_PATH, cmd.imageUuid)
        ret = bash_r("mount | grep %s || mount %s %s" % (mount_path, cache_path, mount_path))
        if ret != 0:
            raise PxeServerError("failed to mount baremetal cache of image[uuid:%s]" % cmd.imageUuid)

        return json_object.dumps(rsp)


class PxeServerDaemon(daemon.Daemon):
    def __init__(self, pidfile):
        super(PxeServerDaemon, self).__init__(pidfile)
        self.agent = PxeServerAgent()

    def run(self):
        self.agent.http_server.start()
