# -*- coding: utf-8 -*-
# Copyright 2014, 2015 Cyan, Inc.
# Copyright 2018 Ciena Corporation

import logging
import os
import os.path
import shutil
import subprocess
import tempfile
import uuid

from urlparse import urlparse
from .service import ExternalService, SpawnedService
from .testutil import get_open_port

log = logging.getLogger(__name__)
log.addHandler(logging.NullHandler())


class Fixture(object):
    kafka_version = os.environ.get('KAFKA_VERSION', '0.9.0.1')
    scala_version = os.environ.get("SCALA_VERSION", '2.10.0')
    project_root = os.environ.get(
        'PROJECT_ROOT', os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../..")))
    kafka_root = os.environ.get(
        "KAFKA_ROOT", os.path.join(project_root, 'servers',
                                   kafka_version, "kafka-bin"))

    @classmethod
    def test_resource(cls, filename):
        return os.path.join(cls.project_root, "servers", cls.kafka_version,
                            "resources", filename)

    @classmethod
    def kafka_run_class_args(cls, *args):
        result = [os.path.join(cls.kafka_root, 'bin', 'kafka-run-class.sh')]
        result.extend(args)
        return result

    @classmethod
    def kafka_run_class_env(cls):
        env = os.environ.copy()
        env['KAFKA_LOG4J_OPTS'] = "-Dlog4j.configuration=file:{}".format(
            cls.test_resource("log4j.properties"))
        return env

    @classmethod
    def render_template(cls, source_file, target_file, binding):
        with open(source_file, "r") as handle:
            template = handle.read()
        with open(target_file, "w") as handle:
            handle.write(template.format(**binding))


class ZookeeperFixture(Fixture):
    @classmethod
    def instance(cls):
        if "ZOOKEEPER_URI" in os.environ:  # pragma: no cover
            parse = urlparse(os.environ["ZOOKEEPER_URI"])
            (host, port) = (parse.hostname, parse.port)
            fixture = ExternalService(host, port)
        else:
            (host, port) = ("127.0.0.1", get_open_port())
            fixture = cls(host, port)

        fixture.open()
        return fixture

    def __init__(self, host, port):
        self.host = host
        self.port = port

        self.tmp_dir = None
        self.child = None

    def out(self, message):
        logging.info("*** Zookeeper [%s:%d]: %s",
                     self.host, self.port, message)

    def open(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.out("Running local instance...")
        logging.info("  host    = %s", self.host)
        logging.info("  port    = %s", self.port)
        logging.info("  tmp_dir = %s", self.tmp_dir)

        # Generate configs
        template = self.test_resource("zookeeper.properties")
        properties = os.path.join(self.tmp_dir, "zookeeper.properties")
        self.render_template(template, properties, vars(self))

        # Configure Zookeeper child process
        args = self.kafka_run_class_args(
            "org.apache.zookeeper.server.quorum.QuorumPeerMain", properties)
        env = self.kafka_run_class_env()
        self.child = SpawnedService(args, env, tag='Zk')

        # Party!
        self.out("Starting...")
        self.child.start()
        self.child.wait_for(
            r"binding to port /127.0.0.1:|Starting server.*ZooKeeperServerMain"
            )
        self.out("Done!")

    def close(self):
        self.out("Stopping...")
        self.child.stop()
        self.child = None
        self.out("Done!")
        shutil.rmtree(self.tmp_dir)


class KafkaFixture(Fixture):

    @classmethod
    def instance(cls, broker_id, zk_host, zk_port,
                 zk_chroot=None, replicas=1, partitions=2,
                 message_max_bytes=1000000):
        if zk_chroot is None:
            zk_chroot = "afkak_" + str(uuid.uuid4()).replace("-", "_")
        if "KAFKA_URI" in os.environ:  # pragma: no cover
            parse = urlparse(os.environ["KAFKA_URI"])
            (host, port) = (parse.hostname, parse.port)
            fixture = ExternalService(host, port)
        else:
            (host, port) = ("127.0.0.1", get_open_port())
            fixture = KafkaFixture(host, port, broker_id, zk_host,
                                   zk_port, zk_chroot, replicas, partitions,
                                   message_max_bytes)
            fixture.open()
        return fixture

    def __init__(self, host, port, broker_id, zk_host,
                 zk_port, zk_chroot, replicas=1, partitions=2,
                 message_max_bytes=1000000):
        self.host = host
        self.port = port

        self.broker_id = broker_id

        self.zk_host = zk_host
        self.zk_port = zk_port
        self.zk_chroot = zk_chroot

        self.replicas = replicas
        self.partitions = partitions
        self.min_insync_replicas = replicas // 2 + 1

        self.message_max_bytes = message_max_bytes

        self.tmp_dir = None
        self.child = None
        self.running = False
        self.restartable = False  # Only restartable after stop() call

    def out(self, message):
        logging.info("*** Kafka [%s:%d]: %s", self.host, self.port, message)

    def open(self):
        if self.running:  # pragma: no cover
            self.out("Instance already running")
            return

        self.tmp_dir = tempfile.mkdtemp()
        self.out("Running local instance...")
        log.info("  host       = %s", self.host)
        log.info("  port       = %s", self.port)
        log.info("  broker_id  = %s", self.broker_id)
        log.info("  zk_host    = %s", self.zk_host)
        log.info("  zk_port    = %s", self.zk_port)
        log.info("  zk_chroot  = %s", self.zk_chroot)
        log.info("  replicas   = %s", self.replicas)
        log.info("  partitions = %s", self.partitions)
        log.info("  msg_max_sz = %s", self.message_max_bytes)
        log.info("  tmp_dir    = %s", self.tmp_dir)

        # Create directories
        os.mkdir(os.path.join(self.tmp_dir, "logs"))
        os.mkdir(os.path.join(self.tmp_dir, "data"))

        # Generate configs
        template = self.test_resource("kafka.properties")
        properties = os.path.join(self.tmp_dir, "kafka.properties")
        self.render_template(template, properties, vars(self))

        # Configure Kafka child process
        self.child_args = self.kafka_run_class_args("kafka.Kafka", properties)
        self.child_env = self.kafka_run_class_env()
        self.child = SpawnedService(self.child_args, self.child_env, tag='Kfa')

        # Party!
        self.out("Creating Zookeeper chroot node...")
        args = self.kafka_run_class_args("org.apache.zookeeper.ZooKeeperMain",
                                         "-server", "%s:%d" % (self.zk_host,
                                                               self.zk_port),
                                         "create",
                                         "/%s" % self.zk_chroot,
                                         "afkak")
        env = self.kafka_run_class_env()
        proc = subprocess.Popen(args, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE)

        if proc.wait() != 0:  # pragma: no cover
            self.out("Failed to create Zookeeper chroot node")
            self.out(proc.stdout)
            self.out(proc.stderr)
            raise RuntimeError("Failed to create Zookeeper chroot node")
        self.out("Done!")

        self.out("Starting...")
        self.child.start()
        self.child.wait_for(r"\[Kafka Server %d\], Started" % self.broker_id)
        self.out("Done!")
        self.running = True

    def close(self):
        if not self.running:  # pragma: no cover
            self.out("Instance already stopped")
            return

        import datetime

        self.out("Stopping... %s at %s" % (
            self.tmp_dir, datetime.datetime.now().isoformat()))
        self.child.stop()
        self.child = None
        self.out("Done!")
        shutil.rmtree(self.tmp_dir)
        self.running = False

    def stop(self):
        """Stop/cleanup the child SpawnedService"""
        self.child.stop()
        self.child = None
        self.out("Child stopped.")
        self.running = False
        self.restartable = True

    def restart(self):
        """Start a new child SpawnedService with same settings and tmpdir"""
        if not self.restartable:  # pragma: no cover
            logging.error("*** Kafka [%s:%d]: %s", self.host, self.port,
                          "Restart attempted when not stopped.")
            return
        self.child = SpawnedService(self.child_args, self.child_env, tag='Kfa')
        self.out("Starting...")
        self.child.start()
        self.child.wait_for(r"\[Kafka Server %d\], Started" % self.broker_id)
        self.out("Done!")
        self.running = True
        self.restartable = False
