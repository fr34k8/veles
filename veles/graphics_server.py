"""
Created on Mar 7, 2014

Copyright (c) 2013 Samsung Electronics Co., Ltd.
"""


import argparse
import array
import errno
import fcntl
import os
import six
from six.moves import cPickle as pickle, zip
import snappy
import socket
import struct
import subprocess
import sys
from tempfile import mkdtemp
from twisted.internet import reactor
import zmq

from veles.cmdline import CommandLineArgumentsRegistry
from veles.compat import from_none
from veles.config import root
from veles.external.txzmq import ZmqConnection, ZmqEndpoint
from veles.logger import Logger
import veles.graphics_client as graphics_client
from veles.paths import __root__


class ZmqPublisher(ZmqConnection):
    socketType = zmq.PUB

    def send(self, message):
        super(ZmqPublisher, self).send(b'graphics' + message)


@six.add_metaclass(CommandLineArgumentsRegistry)
class GraphicsServer(Logger):
    """
    Graphics server which uses ZeroMQ PUB socket to publish updates.
    """

    class InitializationError(Exception):
        pass

    _instance = None
    _pair_fds = {}

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super(GraphicsServer, cls).__new__(cls)
            cls._instance.initialized = False
        return cls._instance

    def __init__(self, thread_pool=None):
        if self.initialized:
            return
        self.initialized = True
        assert thread_pool is not None, (
            "GraphicsServer was not previously initialized")
        super(GraphicsServer, self).__init__()
        parser = GraphicsServer.init_parser()
        args, _ = parser.parse_known_args()
        self._debug_pickle = args.graphics_pickle_debug
        zmq_endpoints = [ZmqEndpoint("bind", "inproc://veles-plots"),
                         ZmqEndpoint("bind", "rndipc://veles-ipc-plots-:")]
        interfaces = []
        for iface, _ in GraphicsServer.interfaces():
            interfaces.append(iface)
            zmq_endpoints.append(ZmqEndpoint(
                "bind", "rndepgm://%s;%s:1024:65535:1" %
                        (iface, root.common.graphics_multicast_address)))
        self.debug("Trying to bind to %s...", zmq_endpoints)
        try:
            self.zmq_connection = ZmqPublisher(zmq_endpoints)
        except zmq.error.ZMQError:
            self.exception("Failed to bind to %s", zmq_endpoints)
            raise from_none(GraphicsServer.InitializationError())

        thread_pool.register_on_shutdown(self.shutdown)

        # tmpfn, *ports = self.zmq_connection.rnd_vals
        tmpfn = self.zmq_connection.rnd_vals[0]
        ports = self.zmq_connection.rnd_vals[1:]

        self.endpoints = {"inproc": "inproc://veles-plots",
                          "ipc": "ipc://" + tmpfn,
                          "epgm": []}
        for port, iface in zip(ports, interfaces):
            self.endpoints["epgm"].append(
                "epgm://%s;%s:%d" %
                (iface, root.common.graphics_multicast_address, port))
        self.info("Publishing to %s", "; ".join([self.endpoints["inproc"],
                                                 self.endpoints["ipc"]] +
                                                self.endpoints["epgm"]))

    @staticmethod
    def init_parser(parser=None):
        parser = parser or argparse.ArgumentParser()
        parser.add_argument("--graphics-pickle-debug", default=False,
                            action="store_true",
                            help="Save plotter object trees during server "
                            "send.")
        return parser

    @staticmethod
    def interfaces():
        max_possible = 128
        max_bytes = max_possible * 32
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        names = array.array('B', b'\0' * max_bytes)
        outbytes = struct.unpack('iL', fcntl.ioctl(
            sock.fileno(),
            0x8912,  # SIOCGIFCONF
            struct.pack('iL', max_bytes, names.buffer_info()[0])
        ))[0]
        sock.close()
        if six.PY3:
            namestr = names.tobytes()
        else:
            namestr = names.tostring()
        for i in range(0, outbytes, 40):
            name = namestr[i:i + 16].split(b'\0', 1)[0]
            if name == b'lo':
                continue
            ip = namestr[i + 20:i + 24]
            yield (name.decode(), ip)

    def enqueue(self, obj):
        data = pickle.dumps(obj)
        if getattr(self, "_debug_pickle", False):
            import objgraph
            restored = pickle.loads(data)
            objgraph.show_refs(restored, too_many=40)
        data = snappy.compress(data)
        self.debug("Broadcasting %d bytes" % len(data))
        zmq_connection = getattr(self, "zmq_connection")
        if zmq_connection is not None:
            zmq_connection.send(data)

    def shutdown(self):
        self.debug("Shutting down")
        self.enqueue(None)

    @staticmethod
    def launch(thread_pool, backend, webagg_callback=None, only_server=False):
        server = GraphicsServer(thread_pool)
        if only_server:
            return server, None
        if six.PY3:
            python = "python3"
        else:
            python = "python"
        args = ["env", python, graphics_client.__file__,
                "--backend", backend, "--endpoint", server.endpoints["ipc"]]
        if backend == "WebAgg" and \
           webagg_callback is not None:
            tmpdir = mkdtemp(prefix="veles-graphics")
            tmpfn = os.path.join(tmpdir, "comm")
            os.mkfifo(tmpfn)
            fifo = os.open(tmpfn, os.O_RDONLY | os.O_NONBLOCK)
            reactor.callLater(0, GraphicsServer._read_webagg_port,
                              fifo, tmpfn, tmpdir, webagg_callback)
            args.append("--webagg-discovery-fifo")
            args.append(tmpfn)
        env = dict(os.environ)
        if 'PYTHONPATH' not in env:
            env['PYTHONPATH'] = __root__
        else:
            env['PYTHONPATH'] += ':' + __root__
        client = subprocess.Popen(args, stdout=sys.stdout, stderr=sys.stderr,
                                  env=env)
        return server, client

    @staticmethod
    def _read_webagg_port(fifo, tmpfn, tmpdir, webagg_callback):
        try:
            output = os.read(fifo, 8)
        except (OSError, IOError) as ioe:
            if ioe.args[0] in (errno.EAGAIN, errno.EINTR):
                output = None
        if not output:
            reactor.callLater(0.1, GraphicsServer._read_webagg_port,
                              fifo, tmpfn, tmpdir, webagg_callback)
        else:
            os.close(fifo)
            os.unlink(tmpfn)
            os.rmdir(tmpdir)
            if webagg_callback is not None:
                webagg_callback(int(output))
