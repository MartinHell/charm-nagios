import subprocess
import socket
import os
import os.path
import re
import sqlite3
import shutil
import tempfile

from pynag import Model

INPROGRESS_DIR = '/etc/nagios3-inprogress'
INPROGRESS_CFG = '/etc/nagios3-inprogress/nagios.cfg'
INPROGRESS_CONF_D = '/etc/nagios3-inprogress/conf.d'
CHARM_CFG = '/etc/nagios3-inprogress/conf.d/charm.cfg'
MAIN_NAGIOS_BAK = '/etc/nagios3.bak'
MAIN_NAGIOS_DIR = '/etc/nagios3'
MAIN_NAGIOS_CFG = '/etc/nagios3/nagios.cfg'
PLUGIN_PATH = '/usr/lib/nagios/plugins'

Model.cfg_file = INPROGRESS_CFG
Model.pynag_directory = INPROGRESS_CONF_D

reduce_RE = re.compile('[\W_]')

def check_ip(n):
    try:
        socket.inet_pton(socket.AF_INET, n)
        return True
    except socket.error:
        try:
            socket.inet_pton(socket.AF_INET6, n)
            return True
        except socket.error:
            return False


def get_ip_and_hostname(remote_unit, relation_id=None):
    args=["relation-get", "private-address", remote_unit]
    if relation_id is not None:
        args.extend(['-r', relation_id])
    hostname = subprocess.check_output(args).strip()
        
    if hostname is None or not len(hostname):
        print "relation-get failed"
        return 2
    if check_ip(hostname):
        # Some providers don't provide hostnames, so use the remote unit name.
        ip_address = hostname
    else:
        ip_address = socket.getaddrinfo(hostname, None)[0][4][0]
    return (ip_address, remote_unit.replace('/', '-'))


def refresh_hostgroups():
    """ Not the most efficient thing but since we're only
        parsing what is already on disk here its not too bad """ 
    hosts = [ x['host_name'] for x in Model.Host.objects.all if x['host_name'] ]

    hgroups = {}
    for host in hosts:
        try:
            (service, unit_id) = host.rsplit('-', 1)
        except ValueError:
            continue
        if service in hgroups:
            hgroups[service].append(host)
        else:
            hgroups[service] = [host]

    # Find existing autogenerated
    auto_hgroups = Model.Hostgroup.objects.filter(notes__contains='#autogenerated#')
    auto_hgroups = [ x.get_attribute('hostgroup_name') for x in auto_hgroups ]

    # Delete the ones not in hgroups
    to_delete = set(auto_hgroups).difference(set(hgroups.keys()))
    for hgroup_name in to_delete:
        try:
            hgroup = Model.Hostgroup.objects.get_by_shortname(hgroup_name)
            hgroup.delete()
        except ValueError:
            pass

    for hgroup_name, members in hgroups.iteritems():
        try:
            hgroup = Model.Hostgroup.objects.get_by_shortname(hgroup_name)
        except ValueError:
            hgroup = Model.Hostgroup()
            hgroup.set_filename(CHARM_CFG)
            hgroup.set_attribute('hostgroup_name', hgroup_name)
            hgroup.set_attribute('notes', '#autogenerated#')

        hgroup.set_attribute('members', ','.join(members))
        hgroup.save()


def _make_check_command(args):
    args = [str(arg) for arg in args]
    # There is some worry of collision, but the uniqueness of the initial
    # command should be enough.
    signature = reduce_RE.sub('_', ''.join(
                [os.path.basename(arg) for arg in args]))
    try:
        cmd = Model.Command.objects.get_by_shortname(signature)
    except ValueError:
        cmd = Model.Command()
        cmd.set_attribute('command_name', signature)
        cmd.set_attribute('command_line', ' '.join(args))
        cmd.save()
    return signature

def _extend_args(args, cmd_args, switch, value):
    args.append(value)
    cmd_args.extend((switch, '"$ARG%d$"' % len(args)))

def customize_http(service, name, extra):
    args = []
    cmd_args = []
    plugin = os.path.join(PLUGIN_PATH, 'check_http')
    port = extra.get('port', 80)
    path = extra.get('path', '/')
    args = [port, path]
    cmd_args = [plugin, '-p', '"$ARG1$"', '-u', '"$ARG2$"']
    if 'status' in extra:
        _extend_args(args, cmd_args, '-e', extra['status'])
    if 'host' in extra:
        _extend_args(args, cmd_args, '-H', extra['host'])
        cmd_args.extend(('-I', '$HOSTADDRESS$'))
    else:
        cmd_args.extend(('-H', '$HOSTADDRESS$'))
    check_command = _make_check_command(cmd_args)
    cmd = '%s!%s' % (check_command, '!'.join([str(x) for x in args]))
    service.set_attribute('check_command', cmd)
    return True


def customize_mysql(service, name, extra):
    plugin = os.path.join(PLUGIN_PATH, 'check_mysql')
    args = []
    cmd_args = [plugin,'-H', '$HOSTADDRESS$']
    if 'user' in extra:
        _extend_args(args, cmd_args, '-u', extra['user'])
    if 'password' in extra:
        _extend_args(args, cmd_args, '-p', extra['password'])
    check_command = _make_check_command(cmd_args)
    cmd = '%s!%s' % (check_command, '!'.join([str(x) for x in args]))
    service.set_attribute('check_command', cmd)
    return True


def customize_nrpe(service, name, extra):
    plugin = os.path.join(PLUGIN_PATH, 'check_nrpe')
    args = []
    cmd_args = [plugin,'-H', '$HOSTADDRESS$']
    if name in ('mem','swap'):
        cmd_args.extend(('-c', 'check_%s' % name))
    elif 'command' in extra:
        cmd_args.extend(('-c', extra['command']))
    else:
        return False
    check_command = _make_check_command(cmd_args)
    cmd = '%s!%s' % (check_command, '!'.join([str(x) for x in args]))
    service.set_attribute('check_command', cmd)
    return True


def customize_service(service, family, name, extra):
    customs = { 'http': customize_http,
                'mysql': customize_mysql,
                'nrpe': customize_nrpe}
    if family in customs:
        return customs[family](service, name, extra)
    return False


def get_pynag_host(target_id, owner_unit=None, owner_relation=None):
    try:
        host = Model.Host.objects.get_by_shortname(target_id)
    except ValueError:
        host = Model.Host()
        host.set_filename(CHARM_CFG)
        host.set_attribute('host_name', target_id)
        host.set_attribute('use', 'generic-host')
        host.save()
        host = Model.Host.objects.get_by_shortname(target_id)
    apply_host_policy(target_id, owner_unit, owner_relation)
    return host


def get_pynag_service(target_id, service_name):
    services = Model.Service.objects.filter(host_name=target_id,
                    service_description=service_name)
    if len(services) == 0:
        service = Model.Service()
        service.set_filename(CHARM_CFG)
        service.set_attribute('service_description', service_name)
        service.set_attribute('host_name', target_id)
        service.set_attribute('use', 'generic-service')
    else:
        service = services[0]
    return service


def apply_host_policy(target_id, owner_unit, owner_relation):
    ssh_service = get_pynag_service(target_id, 'SSH')
    ssh_service.set_attribute('check_command', 'check_ssh')
    ssh_service.save()


def get_valid_relations():
    for x in subprocess.Popen(['relation-ids', 'monitors'], 
        stdout=subprocess.PIPE).stdout:
        yield x.strip()
    for x in subprocess.Popen(['relation-ids', 'nagios'], 
        stdout=subprocess.PIPE).stdout:
        yield x.strip()


def get_valid_units(relation_id):
    for x in subprocess.Popen(['relation-list', '-r', relation_id], 
                              stdout=subprocess.PIPE).stdout:
        yield x.strip()


def _replace_in_config(find_me, replacement):
    with open(INPROGRESS_CFG) as cf:
        with tempfile.NamedTemporaryFile(dir=INPROGRESS_DIR, delete=False) as new_cf:
            for line in cf:
                new_cf.write(line.replace(find_me, replacement))
            new_cf.flush()
            os.chmod(new_cf.name, 0644)
            os.unlink(INPROGRESS_CFG)
            os.rename(new_cf.name, INPROGRESS_CFG)


def initialize_inprogress_config():
    if os.path.exists(INPROGRESS_DIR):
        shutil.rmtree(INPROGRESS_DIR)
    shutil.copytree(MAIN_NAGIOS_DIR, INPROGRESS_DIR)
    _replace_in_config(MAIN_NAGIOS_DIR, INPROGRESS_DIR)
    if os.path.exists(CHARM_CFG):
        os.unlink(CHARM_CFG)


def flush_inprogress_config():
    if not os.path.exists(INPROGRESS_DIR):
        return
    _replace_in_config(INPROGRESS_DIR, MAIN_NAGIOS_DIR)
    if os.path.exists(MAIN_NAGIOS_BAK):
        shutil.rmtree(MAIN_NAGIOS_BAK)
    if os.path.exists(MAIN_NAGIOS_DIR):
        shutil.move(MAIN_NAGIOS_DIR, MAIN_NAGIOS_BAK)
    shutil.move(INPROGRESS_DIR, MAIN_NAGIOS_DIR)
