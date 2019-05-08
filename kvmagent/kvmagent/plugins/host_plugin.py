'''

@author: frank
'''

import platform
from kvmagent import kvmagent
from kvmagent.plugins import vm_plugin
from zstacklib.utils import jsonobject
from zstacklib.utils import http
from zstacklib.utils import lock
from zstacklib.utils import log
from zstacklib.utils import shell
from zstacklib.utils import sizeunit
from zstacklib.utils import linux
from zstacklib.utils import thread
from zstacklib.utils import xmlobject
from zstacklib.utils.bash import *
from zstacklib.utils.report import Report
import os
import os.path
import re
import time
import libvirt
import traceback
import tempfile

IS_AARCH64 = platform.machine() == 'aarch64'

class ConnectResponse(kvmagent.AgentResponse):
    def __init__(self):
        super(ConnectResponse, self).__init__()
        self.iptablesSucc = None

class HostCapacityResponse(kvmagent.AgentResponse):
    def __init__(self):
        super(HostCapacityResponse, self).__init__()
        self.cpuNum = None
        self.cpuSpeed = None
        self.usedCpu = None
        self.totalMemory = None
        self.usedMemory = None
        self.cpuSockets = None

class HostFactResponse(kvmagent.AgentResponse):
    def __init__(self):
        super(HostFactResponse, self).__init__()
        self.osDistribution = None
        self.osVersion = None
        self.osRelease = None
        self.qemuImgVersion = None
        self.libvirtVersion = None
        self.hvmCpuFlag = None
        self.cpuModelName = None

class SetupMountablePrimaryStorageHeartbeatCmd(kvmagent.AgentCommand):
    def __init__(self):
        super(SetupMountablePrimaryStorageHeartbeatCmd, self).__init__()
        self.heartbeatFilePaths = None
        self.heartbeatInterval = None

class SetupMountablePrimaryStorageHeartbeatResponse(kvmagent.AgentResponse):
    def __init__(self):
        super(SetupMountablePrimaryStorageHeartbeatResponse, self).__init__()

class PingResponse(kvmagent.AgentResponse):
    def __init__(self):
        super(PingResponse, self).__init__()
        self.hostUuid = None

class GetUsbDevicesRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(GetUsbDevicesRsp, self).__init__()
        self.usbDevicesInfo = None

class ReportDeviceEventCmd(kvmagent.AgentCommand):
    def __init__(self):
        super(ReportDeviceEventCmd, self).__init__()
        self.hostUuid = None

class UpdateHostOSCmd(kvmagent.AgentCommand):
    def __init__(self):
        super(UpdateHostOSCmd, self).__init__()
        self.hostUuid = None
        self.excludePackages = None

class UpdateHostOSRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(UpdateHostOSRsp, self).__init__()

class UpdateDependencyCmd(kvmagent.AgentCommand):
    def __init__(self):
        super(UpdateDependencyCmd, self).__init__()
        self.hostUuid = None

class UpdateDependencyRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(UpdateDependencyRsp, self).__init__()

class EnableHugePageRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(EnableHugePageRsp, self).__init__()

class CloseHugePageRsp(kvmagent.AgentResponse):
    def __init__(self):
        super(CloseHugePageRsp, self).__init__()

logger = log.get_logger(__name__)

def _get_memory(word):
    out = shell.call("grep '%s' /proc/meminfo" % word)
    (name, capacity) = out.split(':')
    capacity = re.sub('[k|K][b|B]', '', capacity).strip()
    #capacity = capacity.rstrip('kB').rstrip('KB').rstrip('kb').strip()
    return sizeunit.KiloByte.toByte(long(capacity))

def _get_total_memory():
    return _get_memory('MemTotal')

def _get_free_memory():
    return _get_memory('MemFree')

def _get_used_memory():
    return _get_total_memory() - _get_free_memory()

class HostPlugin(kvmagent.KvmAgent):
    '''
    classdocs
    '''

    CONNECT_PATH = '/host/connect'
    CAPACITY_PATH = '/host/capacity'
    ECHO_PATH = '/host/echo'
    FACT_PATH = '/host/fact'
    PING_PATH = "/host/ping"
    GET_USB_DEVICES_PATH = "/host/usbdevice/get"
    SETUP_MOUNTABLE_PRIMARY_STORAGE_HEARTBEAT = "/host/mountableprimarystorageheartbeat"
    UPDATE_OS_PATH = "/host/updateos"
    UPDATE_DEPENDENCY = "/host/updatedependency"
    ENABLE_HUGEPAGE = "/host/enable/hugepage"
    DISABLE_HUGEPAGE = "/host/disable/hugepage"

    def _get_libvirt_version(self):
        ret = shell.call('libvirtd --version')
        return ret.split()[-1]

    def _get_qemu_version(self):
        # to be compatible with both `2.6.0` and `2.9.0(qemu-kvm-ev-2.9.0-16.el7_4.8.1)`
        ret = shell.call('%s -version' % kvmagent.get_qemu_path())
        words = ret.split()
        for w in words:
            if w == 'version':
                return words[words.index(w)+1].strip().split('(')[0]

        raise kvmagent.KvmError('cannot get qemu version[%s]' % ret)

    def _prepare_firewall_for_migration(self):
        """Prepare firewall rules for libvirt live migration."""

        mrule = "-A INPUT -p tcp -m tcp --dport 49152:49261 -j ACCEPT"
        rules = bash_o("iptables -w -S INPUT").splitlines()
        if not mrule in rules:
            bash_r("iptables -w %s" % mrule.replace("-A ", "-I "))

    @lock.file_lock('/run/xtables.lock')
    @in_bash
    def apply_iptables_rules(self, rules):
        logger.debug("starting add iptables rules : %s" % rules)
        if len(rules) != 0 and rules is not None:
            for item in rules:
                rule = item.strip("'").strip('"')
                clean_rule = ' '.join(rule.split(' ')[1:])
                ret = bash_r("iptables -C %s " % clean_rule)
                if ret == 0:
                    continue
                elif ret == 1:
                    # didn't find this rule
                    set_rules_ret = bash_r("iptables %s" % rule)
                    if set_rules_ret != 0:
                        raise Exception('cannot set iptables rule: %s' % rule)
                else:
                    raise Exception('check iptables rule: %s failed' % rule)
        return True

    @kvmagent.replyerror
    def connect(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = ConnectResponse()

        # page table extension
        if shell.run('lscpu | grep -q -w GenuineIntel') == 0:
            new_ept = False if cmd.pageTableExtensionDisabled else True
            rsp.error = self._set_intel_ept(new_ept)
            if rsp.error is not None:
                rsp.success = False
                return jsonobject.dumps(rsp)

        self.host_uuid = cmd.hostUuid
        self.config[kvmagent.HOST_UUID] = self.host_uuid
        self.config[kvmagent.SEND_COMMAND_URL] = cmd.sendCommandUrl
        Report.serverUuid = self.host_uuid
        Report.url = cmd.sendCommandUrl
        logger.debug(http.path_msg(self.CONNECT_PATH, 'host[uuid: %s] connected' % cmd.hostUuid))
        rsp.libvirtVersion = self.libvirt_version
        rsp.qemuVersion = self.qemu_version

        # create udev rule
        self.handle_usb_device_events()

        ignore_msrs = 1 if cmd.ignoreMsrs else 0
        shell.run("/bin/echo %s > /sys/module/kvm/parameters/ignore_msrs" % ignore_msrs)

        vm_plugin.cleanup_stale_vnc_iptable_chains()
        apply_iptables_result = self.apply_iptables_rules(cmd.iptablesRules)
        rsp.iptablesSucc = apply_iptables_result
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def ping(self, req):
        rsp = PingResponse()
        rsp.hostUuid = self.host_uuid
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    def echo(self, req):
        logger.debug('get echoed')
        loop = 0
        while linux.fake_dead('kvmagent') is True and loop < 1200:
            logger.debug('checked fake dead, sleep 3 secs')
            time.sleep(3)
            loop += 1
        return ''

    @kvmagent.replyerror
    def fact(self, req):
        rsp = HostFactResponse()
        rsp.osDistribution, rsp.osVersion, rsp.osRelease = platform.dist()
        # to be compatible with both `2.6.0` and `2.9.0(qemu-kvm-ev-2.9.0-16.el7_4.8.1)`
        qemu_img_version = shell.call("qemu-img --version | grep 'qemu-img version' | cut -d ' ' -f 3 | cut -d '(' -f 1")
        qemu_img_version = qemu_img_version.strip('\t\r\n ,')
        ipV4Addrs = shell.call("ip addr | grep -w inet | grep -v 127.0.0.1 | awk '!/zs$/{print $2}' | cut -d/ -f1")
        system_product_name = shell.call('dmidecode -s system-product-name').strip()
        baseboard_product_name = shell.call('dmidecode -s baseboard-product-name').strip()

        rsp.qemuImgVersion = qemu_img_version
        rsp.libvirtVersion = self.libvirt_version
        rsp.ipAddresses = ipV4Addrs.splitlines()
        rsp.systemProductName = system_product_name if system_product_name else baseboard_product_name
        if not rsp.systemProductName:
            rsp.systemProductName = 'unknown'

        if IS_AARCH64:
            # FIXME how to check vt of aarch64?
            rsp.hvmCpuFlag = 'vt'
            cpu_model = None
            try:
                cpu_model = self._get_host_cpu_model()
            except AttributeError:
                logger.debug("maybe XmlObject has no attribute model, use uname -p to get one")
                if cpu_model is None:
                    cpu_model = os.uname()[-1]

            rsp.cpuModelName = cpu_model
            rsp.hostCpuModelName = "aarch64"

            cpuMHz = shell.call("lscpu | awk '/max MHz/{ print $NF }'")
            # in case lscpu doesn't show cpu max mhz
            cpuMHz = "2500.0000" if cpuMHz.strip() == '' else cpuMHz
            rsp.cpuGHz = '%.2f' % (float(cpuMHz) / 1000)
        else:
            if shell.run('grep vmx /proc/cpuinfo') == 0:
                rsp.hvmCpuFlag = 'vmx'

            if not rsp.hvmCpuFlag:
                if shell.run('grep svm /proc/cpuinfo') == 0:
                    rsp.hvmCpuFlag = 'svm'

            rsp.cpuModelName = self._get_host_cpu_model()

            host_cpu_info = shell.call("grep -m2 -P -o '(model name|cpu MHz)\s*:\s*\K.*' /proc/cpuinfo").splitlines()
            host_cpu_model_name = host_cpu_info[0]
            rsp.hostCpuModelName = host_cpu_model_name

            transient_cpuGHz = '%.2f' % (float(host_cpu_info[1]) / 1000)
            static_cpuGHz_re = re.search('[0-9.]*GHz', host_cpu_model_name)
            rsp.cpuGHz = static_cpuGHz_re.group(0)[:-3] if static_cpuGHz_re else transient_cpuGHz

        return jsonobject.dumps(rsp)

    @vm_plugin.LibvirtAutoReconnect
    def _get_host_cpu_model(conn):
        xml_object = xmlobject.loads(conn.getCapabilities())
        return str(xml_object.host.cpu.model.text_)


    @kvmagent.replyerror
    @in_bash
    def capacity(self, req):
        rsp = HostCapacityResponse()
        rsp.cpuNum = linux.get_cpu_num()
        rsp.cpuSpeed = linux.get_cpu_speed()
        (used_cpu, used_memory) = vm_plugin.get_cpu_memory_used_by_running_vms()
        rsp.usedCpu = used_cpu
        rsp.totalMemory = _get_total_memory()
        rsp.usedMemory = used_memory

        sockets = bash_o('grep "physical id" /proc/cpuinfo | sort -u | wc -l').strip('\n')
        rsp.cpuSockets = int(sockets)
        if rsp.cpuSockets == 0:
            rsp.cpuSockets = 1

        ret = jsonobject.dumps(rsp)
        logger.debug('get host capacity: %s' % ret)
        return ret

    def _heartbeat_func(self, heartbeat_file):
        class Heartbeat(object):
            def __init__(self):
                self.current = None

        hb = Heartbeat()
        hb.current = time.time()
        with open(heartbeat_file, 'w') as fd:
            fd.write(jsonobject.dumps(hb))
        return True

    def _get_intel_ept(self):
        text = None
        with open('/sys/module/kvm_intel/parameters/ept', 'r') as reader:
            text = reader.read()
        return text is None or text.strip() == "Y"

    def _set_intel_ept(self, new_ept):
        error = None
        old_ept = self._get_intel_ept()
        if new_ept != old_ept:
            param = "ept=%d" % new_ept
            if shell.run("modprobe -r kvm-intel") != 0 or shell.run("modprobe kvm-intel %s" % param) != 0:
                error = "failed to reload kvm-intel, please stop the running VM on the host and try again."
            else:
                with open('/etc/modprobe.d/intel-ept.conf', 'w') as writer:
                    writer.write("options kvm_intel %s" % param)
                logger.info("_set_intel_ept(%s) OK." % new_ept)

        if error is not None:
            logger.warn("_set_intel_ept: %s" % error)
        return error

    @kvmagent.replyerror
    def setup_heartbeat_file(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = SetupMountablePrimaryStorageHeartbeatResponse()

        for hb in cmd.heartbeatFilePaths:
            hb_dir = os.path.dirname(hb)
            mount_path = os.path.dirname(hb_dir)
            if not linux.is_mounted(mount_path):
                rsp.error = '%s is not mounted, setup heartbeat file[%s] failed' % (mount_path, hb)
                rsp.success = False
                return jsonobject.dumps(rsp)

        for hb in cmd.heartbeatFilePaths:
            t = self.heartbeat_timer.get(hb, None)
            if t:
                t.cancel()

            hb_dir = os.path.dirname(hb)
            if not os.path.exists(hb_dir):
                os.makedirs(hb_dir, 0755)

            t = thread.timer(cmd.heartbeatInterval, self._heartbeat_func, args=[hb], stop_on_exception=False)
            t.start()
            self.heartbeat_timer[hb] = t
            logger.debug('create heartbeat file at[%s]' % hb)

        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @in_bash
    def get_usb_devices(self, req):
        class UsbDeviceInfo(object):
            def __init__(self):
                self.busNum = ""
                self.devNum = ""
                self.idVendor = ""
                self.idProduct = ""
                self.iManufacturer = ""
                self.iProduct = ""
                self.iSerial = ""
                self.usbVersion = ""
            def toString(self):
                return self.busNum + ':' + self.devNum + ':' + self.idVendor + ':' + self.idProduct + ':' + self.iManufacturer + ':' + self.iProduct + ':' + self.iSerial + ':' + self.usbVersion + ";"

        # use 'lsusb.py -U' to get device ID, like '0751:9842'
        rsp = GetUsbDevicesRsp()
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        r, o, e = bash_roe("lsusb.py -U")
        if r != 0:
            rsp.success = False
            rsp.error = "%s %s" % (e, o)
            return jsonobject.dumps(rsp)

        idSet = set()
        usbDevicesInfo = ''
        for line in o.split('\n'):
            line = line.split()
            if len(line) < 2:
                continue
            idSet.add(line[1])

        for devId in idSet:
            # use 'lsusb -v -d ID' to get device info[s]
            r, o, e = bash_roe("lsusb -v -d %s" % devId)
            if r != 0:
                rsp.success = False
                rsp.error = "%s %s" % (e, o)
                return jsonobject.dumps(rsp)

            for line in o.split('\n'):
                line = line.strip().split()
                if len(line) < 2:
                    continue

                if line[0] == 'Bus':
                    info = UsbDeviceInfo()
                    info.idVendor, info.idProduct = devId.split(':')
                    info.busNum = line[1]
                    info.devNum = line[3].rsplit(':')[0]
                elif line[0] == 'idVendor':
                    info.iManufacturer = ' '.join(line[2:]) if len(line) > 2 else ""
                elif line[0] == 'idProduct':
                    info.iProduct = ' '.join(line[2:]) if len(line) > 2 else ""
                elif line[0] == 'bcdUSB':
                    info.usbVersion = line[1]
                    # special case: USB2.0 with speed 1.5MBit/s or 12MBit/s should be attached to USB1.1 Controller
                    rst = bash_r("lsusb.py | grep -v 'grep' | grep '%s' | grep -E '1.5MBit/s|12MBit/s'" % devId)
                    info.usbVersion = info.usbVersion if rst != 0 else '1.1'
                elif line[0] == 'iManufacturer' and len(line) > 2:
                    info.iManufacturer = ' '.join(line[2:])
                elif line[0] == 'iProduct' and len(line) > 2:
                    info.iProduct = ' '.join(line[2:])
                elif line[0] == 'iSerial':
                    info.iSerial = ' '.join(line[2:]) if len(line) > 2 else ""
                    if info.busNum == '' or info.devNum == '' or info.idVendor == '' or info.idProduct == '':
                        rsp.success = False
                        rsp.error = "cannot get enough info of usb device"
                        return jsonobject.dumps(rsp)
                    else:
                        usbDevicesInfo += info.toString()
        rsp.usbDevicesInfo = usbDevicesInfo
        return jsonobject.dumps(rsp)

    @lock.file_lock('/usr/bin/_report_device_event.sh')
    def handle_usb_device_events(self):
        bash_str = """#!/usr/bin/env python
import urllib2
def post_msg(data, post_url):
    headers = {"content-type": "application/json", "commandpath": "/host/reportdeviceevent"}
    req = urllib2.Request(post_url, data, headers)
    response = urllib2.urlopen(req)
    response.close()

if __name__ == "__main__":
    post_msg("{'hostUuid':'%s'}", '%s')
""" % (self.config.get(kvmagent.HOST_UUID), self.config.get(kvmagent.SEND_COMMAND_URL))

        bash_file = '/usr/bin/_report_device_event.py'
        with open(bash_file, 'w') as f:
            f.write(bash_str)
        os.chmod(bash_file, 0o755)

        rule_str = 'ACTION=="add|remove", SUBSYSTEM=="usb", RUN="%s"' % bash_file
        rule_file = '/etc/udev/rules.d/usb.rules'
        with open(rule_file, 'w') as f:
            f.write(rule_str)

    @kvmagent.replyerror
    @in_bash
    def update_os(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        if not cmd.excludePackages:
            exclude = ""
        else:
            exclude = "--exclude=" + cmd.excludePackages
        yum_cmd = "yum --enablerepo=* clean all && yum --disablerepo=* --enablerepo=zstack-mn,qemu-kvm-ev-mn %s update -y" % exclude

        rsp = UpdateHostOSRsp()
        if shell.run("which yum") != 0:
            rsp.success = False
            rsp.error = "no yum command found, cannot update host os"
        elif shell.run("yum --disablerepo=* --enablerepo=zstack-mn repoinfo") != 0:
            rsp.success = False
            rsp.error = "no zstack-mn repo found, cannot update host os"
        elif shell.run("yum --disablerepo=* --enablerepo=qemu-kvm-ev-mn repoinfo") != 0:
            rsp.success = False
            rsp.error = "no qemu-kvm-ev-mn repo found, cannot update host os"
        elif shell.run(yum_cmd) != 0:
            rsp.success = False
            rsp.error = "failed to update host os using zstack-mn,qemu-kvm-ev-mn repo"
        else:
            logger.debug("successfully run: %s" % yum_cmd)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @in_bash
    def update_dependency(self, req):
        rsp = UpdateDependencyRsp()
        yum_cmd = "yum --enablerepo=* clean all && yum --disablerepo=* --enablerepo=zstack-mn,qemu-kvm-ev-mn install `cat /var/lib/zstack/dependencies` -y"
        if shell.run("which yum") != 0:
            rsp.success = False
            rsp.error = "no yum command found, cannot update kvmagent dependencies"
        elif shell.run("yum --disablerepo=* --enablerepo=zstack-mn repoinfo") != 0:
            rsp.success = False
            rsp.error = "no zstack-mn repo found, cannot update kvmagent dependencies"
        elif shell.run("yum --disablerepo=* --enablerepo=qemu-kvm-ev-mn repoinfo") != 0:
            rsp.success = False
            rsp.error = "no qemu-kvm-ev-mn repo found, cannot update kvmagent dependencies"
        elif shell.run(yum_cmd) != 0:
            rsp.success = False
            rsp.error = "failed to update kvmagent dependencies using zstack-mn,qemu-kvm-ev-mn repo"
        else:
            logger.debug("successfully run: %s" % yum_cmd)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @in_bash
    def disable_hugepage(self, req):
        rsp = CloseHugePageRsp()
        disable_hugepage_script = '''#!/bin/sh
# config nr_hugepages
sysctl -w vm.nr_hugepages=0

# enable nr_hugepages
sysctl vm.nr_hugepages=0

# config grub
sed -i '/GRUB_CMDLINE_LINUX/s/[[:blank:]]*hugepages[[:blank:]]*=[[:blank:]]*[[:graph:]]*//g' /etc/default/grub
sed -i '/GRUB_CMDLINE_LINUX/s/[[:blank:]]*transparent_hugepage[[:blank:]]*=[[:blank:]]*[[:graph:]]*//g' /etc/default/grub
line=`cat /etc/default/grub | grep GRUB_CMDLINE_LINUX`
result=$(echo $line | grep '\"$') 
if [ -n "$result" ]; then 
    grub2-mkconfig -o /boot/grub2/grub.cfg
else 
    sed -i '/GRUB_CMDLINE_LINUX/s/$/\"/g' /etc/default/grub
    grub2-mkconfig -o /boot/grub2/grub.cfg
fi
'''
        fd, disable_hugepage_script_path = tempfile.mkstemp()
        with open(disable_hugepage_script_path, 'w') as f:
            f.write(disable_hugepage_script)
        logger.info('close_hugepage_script_path is: %s' % disable_hugepage_script_path)
        cmd = shell.ShellCmd('bash %s' % disable_hugepage_script_path)
        cmd(False)
        if cmd.return_code != 0 or "Error" in cmd.stdout:
            rsp.success = False
            rsp.error = cmd.stdout
        os.remove(disable_hugepage_script_path)
        return jsonobject.dumps(rsp)

    @kvmagent.replyerror
    @in_bash
    def enable_hugepage(self, req):
        cmd = jsonobject.loads(req[http.REQUEST_BODY])
        rsp = EnableHugePageRsp()
        pageSize = cmd.pageSize
        reserveSize = cmd.reserveSize
        enable_hugepage_script = '''#!/bin/sh
# byte to mib
let "reserveSize=%s/1024/1024"
pageSize=%s
memSize=`free -m | awk '/:/ {print $2;exit}'`
let "pageNum=(memSize-reserveSize)/pageSize"
if [ $memSize -lt $reserveSize ]                                                                                                                                                                                   
then
    echo "Error:reserve size is bigger than system memory size"
    exit 1
fi
#drop cache 
echo 3 > /proc/sys/vm/drop_caches

# enable Transparent HugePages
echo always > /sys/kernel/mm/transparent_hugepage/enabled

# config nr_hugepages
sysctl -w vm.nr_hugepages=$pageNum

# enable nr_hugepages
sysctl vm.nr_hugepages=$pageNum

# config grub
sed -i '/GRUB_CMDLINE_LINUX/s/\"$/ transparent_hugepage=always hugepages=\'\"$pageNum\"\'\"/g' /etc/default/grub
grub2-mkconfig -o /boot/grub2/grub.cfg
''' % (reserveSize, pageSize)

        fd, enable_hugepage_script_path = tempfile.mkstemp()
        with open(enable_hugepage_script_path, 'w') as f:
            f.write(enable_hugepage_script)
        logger.info('enable_hugepage_script_path is: %s' % enable_hugepage_script_path)
        cmd = shell.ShellCmd('bash %s' % enable_hugepage_script_path)
        cmd(False)
        if cmd.return_code != 0 or "Error" in cmd.stdout:
            rsp.success = False
            rsp.error = cmd.stdout
        os.remove(enable_hugepage_script_path)
        return jsonobject.dumps(rsp)

    def start(self):
        self.host_uuid = None

        http_server = kvmagent.get_http_server()
        http_server.register_sync_uri(self.CONNECT_PATH, self.connect)
        http_server.register_async_uri(self.PING_PATH, self.ping)
        http_server.register_async_uri(self.CAPACITY_PATH, self.capacity)
        http_server.register_sync_uri(self.ECHO_PATH, self.echo)
        http_server.register_async_uri(self.SETUP_MOUNTABLE_PRIMARY_STORAGE_HEARTBEAT, self.setup_heartbeat_file)
        http_server.register_async_uri(self.FACT_PATH, self.fact)
        http_server.register_async_uri(self.GET_USB_DEVICES_PATH, self.get_usb_devices)
        http_server.register_async_uri(self.UPDATE_OS_PATH, self.update_os)
        http_server.register_async_uri(self.UPDATE_DEPENDENCY, self.update_dependency)
        http_server.register_async_uri(self.ENABLE_HUGEPAGE, self.enable_hugepage)
        http_server.register_async_uri(self.DISABLE_HUGEPAGE, self.disable_hugepage)

        self.heartbeat_timer = {}
        self.libvirt_version = self._get_libvirt_version()
        self.qemu_version = self._get_qemu_version()
        self._prepare_firewall_for_migration()
        filepath = r'/etc/libvirt/qemu/networks/autostart/default.xml'
        if os.path.exists(filepath):
            os.unlink(filepath)

    def stop(self):
        pass

    def configure(self, config):
        self.config = config
