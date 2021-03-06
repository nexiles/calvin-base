# -*- coding: utf-8 -*-

# Copyright (c) 2015 Ericsson AB
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import time
import traceback
import platform
import random
import socket

from calvin.utilities import calvinlogger

from calvin.runtime.south.plugins.storage.twistedimpl.dht.service_discovery import ServiceDiscoveryBase

from twisted.internet.protocol import DatagramProtocol
from twisted.web.http import datetimeToString
from twisted.internet import reactor, defer

_log = calvinlogger.get_logger(__name__)

SSDP_ADDR = '239.255.255.250'
SSDP_PORT = 1900

__version_info__ = (0, 6, 7)
__version__ = '.'.join(map(str, __version_info__))

SERVER_ID = ','.join([platform.system(),
                      platform.release(),
                      'UPnP/1.0,Calvin UPnP framework',
                      __version__])
SERVICE_UUID = '1693326a-abb9-11e4-8dfb-9cb654a16426'

MS =    ('M-SEARCH * HTTP/1.1\r\nHOST: %s:%d\r\nMAN: "ssdp:discover"\r\n' +
         'MX: 2\r\nST: uuid:%s\r\n\r\n') %\
        (SSDP_ADDR, SSDP_PORT, SERVICE_UUID)

MS_RESP =   'HTTP/1.1 200 OK\r\n' + \
            'USN: %s::upnp:rootdevice\r\n' % SERVICE_UUID + \
            'SERVER: %s\r\nlast-seen: %s\r\nEXT: \r\nSERVICE: %s\r\n' + \
            'LOCATION: http://calvin@github.se/%s/description-0.0.1.xml\r\n' % SERVICE_UUID + \
            'CACHE-CONTROL: max-age=1800\r\nST: uuid:%s\r\n' % SERVICE_UUID + \
            'DATE: %s\r\n\r\n'


def parse_http_response(data):

    """ don't try to get the body, there are reponses without """
    header = data.split('\r\n\r\n')[0]

    lines = header.split('\r\n')
    cmd = lines[0].split(' ')
    lines = map(lambda x: x.replace(': ', ':', 1), lines[1:])
    lines = filter(lambda x: len(x) > 0, lines)

    headers = [x.split(':', 1) for x in lines]
    headers = dict(map(lambda x: (x[0].lower(), x[1]), headers))

    return cmd, headers


class ServerBase(DatagramProtocol):
    def __init__(self, d=None):
        self._services = {}
        self._dstarted = d

    def startProtocol(self):
        if self._dstarted:
            reactor.callLater(0, self._dstarted.callback, True)

    def datagramReceived(self, datagram, address):
        # Broadcast
        try:
            cmd, headers = parse_http_response(datagram)
            _log.debug("Received %s, %s from %r" % (cmd, headers, address, ))

            if cmd[0] == 'M-SEARCH' and cmd[1] == '*':

                # Only reply to our requests
                if SERVICE_UUID in headers['st']:

                    for k, v in self._services.items():
                        addr = v
                        # Ignore 0.0.0.0, use the ip we where contacted on
                        if addr[0] == "0.0.0.0":
                            addr = (address[0], addr[1])

                        response = MS_RESP % ('%s:%d' % addr, str(time.time()),
                                              k, datetimeToString())

                        delay = random.randint(0, min(5, int(headers['mx'])))
                        reactor.callLater(delay, self.send_it,
                                          response, address)
        except:
            _log.exception("Error datagram recieved")

    def add_service(self, service, ip, port):
        self._services[service] = (ip, port)

    def remove_service(self, service):
        if service in self._services:
            del self._services[service]

    def send_it(self, response, destination):
        try:
            if self.transport:
                self.transport.write(response, destination)
            else:
                _log.debug("No transport yet!")
        except (AttributeError, socket.error), msg:
            _log.exception("Error in send %s" % repr(msg))

    def stop(self):
        pass


class ClientBase(DatagramProtocol):
    def __init__(self, d=None):
        self._service = None
        self._mserach_cb = None
        self._msearch_stopped = False
        self._msearch_stop = False
        self._dstarted = d
        self._msearch_cb = None

    def startProtocol(self):
        if self._dstarted:
            reactor.callLater(0, self._dstarted.callback, True)

    def datagramReceived(self, datagram, address):
        # Broadcast
        cmd, headers = parse_http_response(datagram)

        if cmd[0].startswith('HTTP/1.') and cmd[1] == '200':

            _log.debug("Received %s from %r" % (headers, address, ))
            if SERVICE_UUID in headers['st']:
                c_address = headers['server'].split(':')
                c_address[1] = int(c_address[1])

                # Filter on service calvin networks
                if self._service is None or \
                   self._service == headers['service']:

                    _log.debug("Received service %s from %s" %
                               (headers['service'], c_address, ))

                    if c_address:
                        if self._msearch_cb:
                            self._msearch_cb([tuple(c_address)])
                        if self._msearch_stop:
                            self.stop()

    def set_callback(self, callback):
        self._msearch_cb = callback

    def set_service(self, service):
        self._service = service

    def is_stopped(self):
        return self._msearch_stopped

    def set_autostop(self, stop=True):
        self._msearch_stop = stop

    def stop(self):
        self._msearch_stopped = True


class SSDPServiceDiscovery(ServiceDiscoveryBase):
    def __init__(self, iface):
        super(SSDPServiceDiscovery, self).__init__()
        self.iface = iface
        self.ssdp = None
        self.port = None
        self._backoff = .2

    def start(self):
        dserver = defer.Deferred()
        dclient = defer.Deferred()
        try:
            self.ssdp = reactor.listenMulticast(SSDP_PORT, ServerBase(d=dserver),
                                                listenMultiple=True)
            self.ssdp.setLoopbackMode(1)
            self.ssdp.joinGroup(SSDP_ADDR, interface=self.iface)
        except:
            _log.exception("Multicast listen join failed!!")
            # Dont start server some one is alerady running locally

        self.port = reactor.listenUDP(0, ClientBase(d=dclient), interface=self.iface)
        _log.debug("SSDP Host: %s" % repr(self.port.getHost()))

        return dserver, dclient

    def start_search(self, callback=None, stop=False):

        # Restart backoff
        self._backoff = .2

        def local_start_msearch(stop):
            self.port.protocol.set_callback(callback)
            self.port.protocol.set_autostop(stop)
            self._send_msearch(once=False)

        reactor.callLater(0, local_start_msearch, stop=stop)

    def stop_search(self):
        self.port.protocol.set_callback(None)
        self.port.protocol.stop()

    def set_client_filter(self, service):
        self.port.protocol.set_service(service)

    def register_service(self, service, ip, port):
        self.ssdp.protocol.add_service(service, ip, port)

    def unregister_service(self, service):
        self.ssdp.protocol.remove_service(service)

    def _send_msearch(self, once=True):
        if self.port:
            _log.debug("Sending M-SEARCH...")
            self.port.write(MS, (SSDP_ADDR, SSDP_PORT))
            if not once and self.port is not None and not self.port.protocol.is_stopped():
                reactor.callLater(self._backoff, self._send_msearch, once=False)
                self._backoff = min(10, self._backoff * 1.5)
                _log.debug("backoff %s" % self._backoff)
        else:
            _log.debug(traceback.format_stack())

    def search(self):
        self._send_msearch(once=True)

    def stop(self):
        dlist = []
        if self.ssdp:
            dlist.append(self.ssdp.leaveGroup(SSDP_ADDR, interface=self.iface))
            dlist.append(self.ssdp.stopListening())
            self.ssdp = None
        if self.port:
            self.stop_search()
            dlist.append(self.port.stopListening())
            self.port = None
        return defer.DeferredList(dlist)
