#!/usr/bin/python3
# -*- coding: utf-8; tab-width: 4; indent-tabs-mode: t -*-

import os
import re
import uuid
from OpenSSL import crypto
from gbs_util import GbsUtil
from gbs_param import GbsConst


class GbsProtocolException(Exception):
    pass


class GbsBusinessException(Exception):
    pass


class GbsPluginApi:

    ProtocolException = GbsProtocolException
    BusinessException = GbsBusinessException

    def __init__(self, param, sessObj):
        self.param = param
        self.sessObj = sessObj

        self.procDir = os.path.join(self.sessObj.sysObj.getMntDir(), "proc")
        self.sysDir = os.path.join(self.sessObj.sysObj.getMntDir(), "sys")
        self.devDir = os.path.join(self.sessObj.sysObj.getMntDir(), "dev")
        self.runDir = os.path.join(self.sessObj.sysObj.getMntDir(), "run")
        self.tmpDir = os.path.join(self.sessObj.sysObj.getMntDir(), "tmp")
        self.varDir = os.path.join(self.sessObj.sysObj.getMntDir(), "var")
        self.varTmpDir = os.path.join(self.varDir, "tmp")
        self.homeDirForRoot = os.path.join(self.sessObj.sysObj.getMntDir(), "root")
        self.lostFoundDir = os.path.join(self.sessObj.sysObj.getMntDir(), "lost+found")

        self.hasHomeDirForRoot = False
        self.hasVarDir = False

    def getUuid(self):
        return self.sessObj.sysObj.getUuid()

    def getCpuArch(self):
        return self.sessObj.sysObj.getClientInfo().cpu_arch

    def getIpAddress(self):
        return self.sessObj.sslSock.getpeername()[0]

    def getCertificate(self):
        return self.sessObj.sslSock.get_peer_certificate()

    def getPublicKey(self):
        return self.sessObj.sysObj.pubkey

    def getRootDir(self):
        return self.sessObj.sysObj.getMntDir()


class GbsPluginManager:

    @staticmethod
    def getPluginNameList():
        ret = []
        for fn in os.listdir(GbsConst.pluginsDir):
            if fn == "__pycache__":
                continue
            if os.path.isdir(fn):
                ret.append(fn)
            else:
                ret.append(fn.replace(".py", ""))
        return ret

    @staticmethod
    def loadPluginObject(pluginName, param, ctrlSession):
        exec("import plugins.%s" % (pluginName))
        return eval("plugins.%s.PluginObject(param, GbsPluginApi(param, ctrlSession))" % (pluginName))


class GbsClientInfo:

    def __init__(self):
        self.hostname = None
        self.cpu_arch = None
        self.capacity = None            # how much harddisk this client occupy
        self.ssh_pubkey = None


class GbsSystemDatabase:

    @staticmethod
    def getUuidList(param):
        if not os.path.exists(param.cacheDir):
            return []
        return os.listdir(param.cacheDir)

    @staticmethod
    def getClientInfo(param, uuid):
        ret = GbsClientInfo()

        with open(_info_file(param, uuid), "r") as f:
            buf = f.read()
            m = re.match("^hostname = (.*)$", buf, re.M)
            if m is not None:
                ret.hostname = m.group(1)

        ret.capacity = os.path.getsize(_image_file(param, uuid))

        with open(_ssh_pubkey_file(param, uuid), "r") as f:
            ret.ssh_pubkey = f.read()

        return ret


class GbsSystem:

    def __init__(self, param, pubkey):
        self.param = param
        self.uuid = None
        self.sshPubKeyFile = None
        self.imageFile = None
        self.infoFile = None
        self.mntDir = None
        self.clientInfo = None
        self.loopDev = None

        pubkey = crypto.dump_publickey(crypto.FILETYPE_PEM, pubkey)

        # ensure cache directory exists
        if not os.path.exists(self.param.cacheDir):
            os.makedirs(self.param.cacheDir)

        # find system
        for oldUuid in os.listdir(self.param.cacheDir):
            with open(_ssh_pubkey_file(self.param, oldUuid), "rb") as f:
                if pubkey == f.read():
                    self.uuid = oldUuid
                    self.sshPubKeyFile = _ssh_pubkey_file(self.param, self.uuid)
                    self.imageFile = _image_file(self.param, self.uuid)
                    self.infoFile = _info_file(self.param, self.uuid)
                    self.mntDir = _mnt_dir(self.param, self.uuid)
                    self._loadClientInfo(pubkey)
                    return

        # create new system
        self.uuid = uuid.uuid4().hex
        dirname = os.path.join(self.param.cacheDir, self.uuid)
        os.makedirs(dirname)

        # record public key
        self.sshPubKeyFile = _ssh_pubkey_file(self.param, self.uuid)
        with open(self.sshPubKeyFile, "wb") as f:
            f.write(pubkey)

        # generate disk image
        self.imageFile = _image_file(self.param, self.uuid)
        GbsUtil.shell("/bin/dd if=/dev/zero of=%s bs=%d count=%d conv=sparse" % (self.imageFile, _mb(), GbsConst.imageSizeInit), "stdout")
        GbsUtil.shell("/sbin/mkfs.ext4 -O ^has_journal %s" % (self.imageFile), "stdout")

        # create information file
        self.infoFile = _info_file(self.param, self.uuid)
        GbsUtil.touchFile(self.infoFile)

        # create mount directory
        self.mntDir = _mnt_dir(self.param, self.uuid)
        GbsUtil.ensureDir(self.mntDir)

        self._loadClientInfo(pubkey)

    def getUuid(self):
        return self.uuid

    def getClientInfo(self):
        return self.clientInfo

    def commitClientInfo(self):
        with open(self.infoFile, "w") as f:
            f.write("hostname = %s\n" % (self.clientInfo.hostname if self.clientInfo.hostname is not None else ""))

    def getMntDir(self):
        return self.mntDir

    def mount(self):
        assert self.loopDev is None

        GbsUtil.shell("/bin/mount %s %s" % (self.imageFile, self.mntDir))
        try:
            out = GbsUtil.shell("/sbin/losetup -j %s" % (self.imageFile), "stdout").decode("utf-8")
            m = re.match("(/dev/loop[0-9]+): .*", out)
            if m is None:
                raise Exception("can not find loop device for mounted disk")
            self.loopDev = m.group(1)
        except:
            GbsUtil.shell("/bin/umount %s" % (self.mntDir))
            raise

    def unmount(self):
        if self.loopDev is None:
            return
        GbsUtil.forceUnmount(self.mntDir)

    def enlarge(self):
        if self.loopDev is None:
            return
        if GbsUtil.getDirFreeSpace(self.mntDir) >= GbsConst.imageSizeMinimalRemain:
            return

        GbsUtil.shell("/bin/dd if=/dev/zero of=%s seek=%d bs=%d count=%d conv=sparse oflag=seek_bytes" % (self.imageFile, os.path.getsize(self.imageFile), _mb(), GbsConst.imageSizeStep), "stdout")
        if self.loopDev is not None:
            GbsUtil.shell("/sbin/losetup -c %s" % (self.loopDev))
        GbsUtil.shell("/sbin/resize2fs %s" % (self.imageFile), "stdout")
        self.clientInfo.capacity = os.path.getsize(self.imageFile)

    def prepareRoot(self):
        assert self.loopDev is not None

        self.procDir = os.path.join(self.mntDir, "proc")
        self.sysDir = os.path.join(self.mntDir, "sys")
        self.devDir = os.path.join(self.mntDir, "dev")
        self.runDir = os.path.join(self.mntDir, "run")
        self.tmpDir = os.path.join(self.mntDir, "tmp")
        self.varDir = os.path.join(self.mntDir, "var")
        self.varTmpDir = os.path.join(self.varDir, "tmp")
        self.homeDirForRoot = os.path.join(self.mntDir, "root")
        self.lostFoundDir = os.path.join(self.mntDir, "lost+found")
        self.hasVarDir = os.path.exists(self.varDir)
        self.hasHomeDirForRoot = os.path.exists(self.homeDirForRoot)

        try:
            if os.path.exists(self.procDir):
                raise Exception("Redundant directory /proc is synced up")
            if os.path.exists(self.sysDir):
                raise Exception("Redundant directory /sys is synced up")
            if os.path.exists(self.devDir):
                raise Exception("Redundant directory /dev is synced up")
            if os.path.exists(self.runDir):
                raise Exception("Redundant directory /run is synced up")
            if os.path.exists(self.tmpDir):
                raise Exception("Redundant directory /tmp is synced up")
            if os.path.exists(self.varTmpDir):
                raise Exception("Redundant directory /var/tmp is synced up")
            if os.path.exists(self.lostFoundDir):
                raise Exception("Directory /lost+found should not exist")

            os.mkdir(self.procDir)
            GbsUtil.shell("/bin/mount -t proc proc %s" % (self.procDir), "stdout")

            os.mkdir(self.sysDir)
            GbsUtil.shell("/bin/mount --rbind /sys %s" % (self.sysDir), "stdout")
            GbsUtil.shell("/bin/mount --make-rslave %s" % (self.sysDir), "stdout")

            os.mkdir(self.devDir)
            GbsUtil.shell("/bin/mount --rbind /dev %s" % (self.devDir), "stdout")
            GbsUtil.shell("/bin/mount --make-rslave %s" % (self.devDir), "stdout")

            os.mkdir(self.runDir)
            GbsUtil.shell("/bin/mount -t tmpfs tmpfs %s -o nosuid,nodev,mode=755" % (self.runDir), "stdout")

            os.mkdir(self.tmpDir)
            os.chmod(self.tmpDir, 0o1777)
            GbsUtil.shell("/bin/mount -t tmpfs tmpfs %s -o nosuid,nodev" % (self.tmpDir), "stdout")

            if not self.hasVarDir:
                os.mkdir(self.varDir)
            os.mkdir(self.varTmpDir)

            if not self.hasHomeDirForRoot:
                os.mkdir(self.homeDirForRoot)
                os.chmod(self.homeDirForRoot, 0o700)
        except:
            self.unPrepareRoot()
            raise

    def unPrepareRoot(self):
        assert self.loopDev is not None

        if not self.hasHomeDirForRoot:
            GbsUtil.forceDelete(self.homeDirForRoot)

        if not self.hasVarDir:
            GbsUtil.forceDelete(self.varDir)
        else:
            GbsUtil.forceDelete(self.varTmpDir)

        if os.path.exists(self.tmpDir):
            GbsUtil.forceUnmount(self.tmpDir)
            os.rmdir(self.tmpDir)

        if os.path.exists(self.runDir):
            GbsUtil.forceUnmount(self.runDir)
            os.rmdir(self.runDir)

        if os.path.exists(self.devDir):
            GbsUtil.shell("/bin/umount -l %s" % (self.devDir))      # devDir is always busy, why?
            os.rmdir(self.devDir)

        if os.path.exists(self.sysDir):
            GbsUtil.shell("/bin/umount -l %s" % (self.sysDir))      # sysDir is always busy, why?
            os.rmdir(self.sysDir)

        if os.path.exists(self.procDir):
            GbsUtil.forceUnmount(self.procDir)
            os.rmdir(self.procDir)

        del self.procDir
        del self.sysDir
        del self.devDir
        del self.runDir
        del self.tmpDir
        del self.varDir
        del self.varTmpDir
        del self.homeDirForRoot
        del self.lostFoundDir
        del self.hasVarDir
        del self.hasHomeDirForRoot

    def _loadClientInfo(self, pubkey):
        self.clientInfo = GbsClientInfo()

        with open(self.infoFile, "r") as f:
            buf = f.read()
            m = re.match("^hostname = (.*)$", buf, re.M)
            if m is not None:
                self.clientInfo.hostname = m.group(1)

        self.clientInfo.capacity = os.path.getsize(self.imageFile)
        self.clientInfo.ssh_pubkey = pubkey


def _mb():
    return 1024 * 1024


def _gb():
    return 1024 * 1024 * 1024


def _info_file(param, uuid):
    return os.path.join(param.cacheDir, uuid, "client-info")


def _image_file(param, uuid):
    return os.path.join(param.cacheDir, uuid, "disk.img")


def _ssh_pubkey_file(param, uuid):
    return os.path.join(param.cacheDir, uuid, "pubkey.pem")


def _mnt_dir(param, uuid):
    return os.path.join(param.cacheDir, uuid, "mntdir")


# @staticmethod
# def findSystemBySshPublicKey(param, key):
#     for fn in os.listdir(param.varDir):
#         if not fn.endswith(".pub"):
#             continue
#         with open(os.path.join(param.varDir, fn, "r")) as f:
#             if f.read() == key:
#                 m = re.search("^(.*)::(.*).pub$", fn)
#                 assert m is not None
#                 return (m.group(1), m.group(2))
#     return None
