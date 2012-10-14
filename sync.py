#!/usr/bin/python

import arlalow
import pprint
import os
import sqlite3
import stat
import tempfile
import sys

from util import *
from afsutil import *

if len(sys.argv) != 2:
    print >> sys.stderr, "usage:", sys.argv[0], "<local directory>"
    sys.exit(1)
basepath = sys.argv[1]

conn = sqlite3.connect(basepath + "/.afssync/db.sqlite")
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute('select volume, cell from config where seq = 1')
(volume, cell) = cursor.fetchone()




print "Initializing"

arlalow.init()

fs = fileserver(cell, volume)

wholetree = fs.traverse_remote_tree(fs.rootfid, "")

print "Fetched tree"

def allFileEntriesFromTree(tree):
    if "children" in tree:
        return [{"name": tree.get("name"), "fid":tree.get("fid"), "path":tree.get("path"), "parent":tree.get("parent")}] + [e for f in tree["children"] for e in allFileEntriesFromTree(f)]
    else:
        return [tree]

serverfiles = allFileEntriesFromTree(wholetree)

#print serverfiles

status = fs.getstatusforfids([e["fid"] for e in serverfiles])

for e in serverfiles:
    e["status"] = status[e["fid"]]


basefiles = []
basefilesdict = {}

def readbasefiles():
    cursor.execute('select vnode, uniq, filetype, dataversion, path, mode, localmodtime, length, checksum from files where deleted is null')
    global basefiles
    basefiles = [dict([(k, row[k]) for k in row.keys()]) for row in cursor.fetchall()]

    # print
    # print "base"
    global basefilesdict
    basefilesdict = dict([(e["path"], e) for e in basefiles])
    # print basefilesdict.keys()

readbasefiles()


localfiles = [e for e in getlocalfiles(basepath) if not e["path"].startswith("/.afssync/")]
#print localfiles

#print
#print "local"
localfilesdict = dict([(e["path"], e) for e in localfiles])
#print localfilesdict.keys()


#print
#print "server"
serverfilesdict = dict([(e["path"], e) for e in serverfiles])
#print serverfilesdict.keys()

allfiles = sorted(set(localfilesdict.keys()) | set(basefilesdict.keys()) | set(serverfilesdict.keys()))

def mode_to_filetype(mode):
    if stat.S_ISREG(mode):
        return 1
    if stat.S_ISDIR(mode):
        return 2
    if stat.S_ISLNK(mode):
        return 3
    raise Exception("Unknown file type: mode %o", mode)

def fetchfile(basepath, serverfile):
    (fd, name) = tempfile.mkstemp(dir=basepath+"/.afssync/fetched")
    fid = serverfile["fid"]

    (status, checksum) = fs.getfile(fid, serverfile["status"]["length"], os.fdopen(fd, "wb"))
    f.close()
    assert serverfile["status"]["length"] == status["length"]
    assert serverfile["status"]["dataversion"] == status["dataversion"]
    status["checksum"] = checksum
    return (name, status)

def sendfileandupdate(basepath, name, localfile, parentfid, serverfile=None):
    # XXX creates and uploads directly to final filename.
    #   advantage: atomically ensures that no file is overwritten
    #   disadvantage: file is visible to other clients directly
    print "send file", name
    localfiletype = mode_to_filetype(localfile["mode"])
    if localfiletype == 1:
        if serverfile == None:
            result = fs.fsconn.create(fsconn, parentfid, localfile["name"], localfile["mode"] & 0777, localfile["mtime"])
            fid = result["fid"]
        else:
            fid = serverfile["fid"]
        contents = readwholefile(basepath + "/" + name)
        status = arlalow.storewholefile(fs.fsconn, fid, contents, localfile["mode"] & 0777, localfile["mtime"])
        (volume, vnode, unique) = fid
        checksum = checksumcontents(contents)
        cursor.execute('delete from files where path = ?', (name,))
        cursor.execute('insert into files (vnode, uniq, filetype, dataversion, path, mode, localmodtime, length, checksum) values (?, ?, ?, ?, ?, ?, ?, ?, ?)', (vnode, unique, localfiletype, status["dataversion"], name, status["mode"] & 0777, status["modtime"], status["length"], checksum))
        conn.commit()
        if not serverfile:
            print "created file with fid", fid
    elif localfiletype == 2:
        print localfile
        result = arlalow.mkdir(fs.fsconn, parentfid, localfile["name"], localfile["mode"] & 0777, localfile["mtime"])
        fid = result["fid"]
        status = result["status"]
        (volume, vnode, unique) = fid
        cursor.execute('delete from files where path = ?', (name,))
        cursor.execute('insert into files (vnode, uniq, filetype, dataversion, path, mode, localmodtime) values (?, ?, ?, ?, ?, ?, ?)', (vnode, unique, localfiletype, status["dataversion"], name, status["mode"] & 0777, status["modtime"]))
        conn.commit()
        print "created directory with fid", fid
    else:
        raise Exception("Cannot handle symlinks yet")

def fetchfileandupdate(basepath, name, serverfile):
    serverfiletype = serverfile["status"]["type"]
    if serverfiletype == 1:
        (tempname, status) = fetchfile(basepath, serverfile)
        os.chmod(tempname, status["mode"] & 0777)
        os.utime(tempname, (status["modtime"], status["modtime"]))
        os.rename(tempname, basepath + "/" + name)
        (volume, vnode, unique) = serverfile["fid"]
        cursor.execute('delete from files where path = ?', (name,))
        cursor.execute('insert into files (vnode, uniq, filetype, dataversion, path, mode, localmodtime, length, checksum) values (?, ?, ?, ?, ?, ?, ?, ?, ?)', (vnode, unique, serverfiletype, status["dataversion"], name, status["mode"] & 0777, status["modtime"], status["length"], status["checksum"]))
        conn.commit()
    elif serverfiletype == 2:
        # XXX doesn't update basefiles list
        status = serverfile["status"]
        (volume, vnode, unique) = serverfile["fid"]
        os.mkdir(basepath + "/" + name)
        os.chmod(basepath + "/" + name, status["mode"] & 0777)
        cursor.execute('insert into files (vnode, uniq, filetype, dataversion, path, mode, localmodtime) values (?, ?, ?, ?, ?, ?, ?)', (vnode, unique, serverfiletype, status["dataversion"], name, status["mode"] & 0777, status["modtime"]))
        conn.commit()
    else:
        raise Exception("Cannot handle symlinks yet")
        

def conflictresolution(basepath, name, localfile, serverfile):
    # XXX doesn't restart the syncing, one file should be sent and one file should be fetched
    localfiletype = mode_to_filetype(localfile["mode"])
    serverfiletype = serverfile["status"]["type"]
    print "resolve file", name, localfiletype, serverfiletype
    if serverfiletype == 1:
        #(tempname, status) = fetchfile(basepath, serverfile)
        if localfile["mtime"] > serverfile["status"]["modtime"]:
            # XXX check that new name is unused
            arlalow.rename(fs.fsconn, serverfile["parent"], serverfile["name"], serverfile["parent"], serverfile["name"] + ";serverrenamed-1")
        else:
            os.rename(basepath + "/" + name, basepath + "/" + name + ";locallyrenamed-1")
    elif serverfiletype == 2:
        raise Exception("Cannot handle directories yet")
    else:
        raise Exception("Cannot handle symlinks yet")

def synconefile(basepath, name, localfile, serverfile, basefile, parentfid):
    localchecksum = checksumfile(basepath + "/" + name) if localfile and stat.S_ISREG(localfile["mode"]) else None
    places = (localfile != None, basefile != None, serverfile != None)
    
    if places == (False, False, True):
        print name, "new on server"
        fetchfileandupdate(basepath, name, serverfile)
        readbasefiles()
#    elif places == (False, True, False):
#        pass
    elif places == (False, True, True):
        if basefile["filetype"] == 1:
            arlalow.remove(fs.fsconn, serverfile["parent"], serverfile["name"])
            cursor.execute('delete from files where path = ?', (name,))
            conn.commit()
            readbasefiles()
        elif basefile["filetype"] == 2:
            def removedir():
                arlalow.rmdir(fs.fsconn, serverfile["parent"], serverfile["name"])
                cursor.execute('delete from files where path = ?', (name,))
                conn.commit()
                readbasefiles()
            return (lambda curpath: not curpath.startswith(name), removedir)
        else:
            raise Exception("Cannot handle symlinks yet")
    elif places == (True, False, False):
        print name, "created on client"
        sendfileandupdate(basepath, name, localfile, parentfid)
        readbasefiles()
    elif places == (True, False, True):
        print name, "created on server and client"
        conflictresolution(basepath, name, localfile, serverfile)
    elif places == (True, True, False):
        print name, "removed from server"
        basefiletype = basefile["filetype"]
        if basefiletype == 1:
            os.rename(basepath + "/" + name, basepath+"/.afssync/trash/%d-%d" % (basefile["vnode"], basefile["uniq"]))
            cursor.execute("update files set deleted = 1 where path = ?", (name, ))
            conn.commit()
            readbasefiles()
        elif basefiletype == 2:
            def removedir():
                os.rmdir(basepath + "/" + name)
                cursor.execute("delete from files where path = ?", (name, ))
                conn.commit()
                readbasefiles()
            return (lambda curpath: not curpath.startswith(name), removedir)
        else:
            raise Exception("Cannot handle symlinks yet")
    elif places == (True, True, True):
        if basefile["filetype"] != serverfile["status"]["type"] or basefile["filetype"] != mode_to_filetype(localfile["mode"]):
            raise Exception(name + ": Cannot handle changed filetypes yet")
        if basefile["filetype"] == 1:
            changes = (localchecksum != basefile["checksum"],
                       (basefile["vnode"], basefile["uniq"], basefile["dataversion"]) != (serverfile["fid"][1], serverfile["fid"][2], serverfile["status"]["dataversion"]))
            if changes == (False, False):
                pass
            elif changes == (False, True):
                fetchfileandupdate(basepath, name, serverfile)
                readbasefiles()
            elif changes == (True, False):
                sendfileandupdate(basepath, name, localfile, parentfid, serverfile=serverfile)
                readbasefiles()
            else:
                raise Exception(name + ": Unhandled changes state " + str(changes))
        elif basefile["filetype"] == 2:
            # only care about changed mode
            # XXX implement mode changing
            pass
        else:
            raise Exception("Cannot handle symlinks yet")
    else:
        raise Exception("Unhandled places state " + str(places))


try:
    delayedtasks = []
    for f in allfiles:
        for delayedtask in delayedtasks:
            (predicate, action) = delayedtask
            if predicate(f):
                action()
                delayedtasks.remove(delayedtask)
        if f == "/":
            continue
        parentpath = os.path.dirname(os.path.normpath(f))
        if parentpath == "/":
            parentfid = fs.rootfid
        else:
            parentbasefile = basefilesdict[parentpath + "/"]
            parentfid = (fs.rootfid[0], parentbasefile["vnode"], parentbasefile["uniq"])
        # print f, parentpath, parentfid
        localfile = localfilesdict.get(f)
        serverfile = serverfilesdict.get(f)
        basefile = basefilesdict.get(f)
        # if localfile:
        #    print "local mtime", localfile["mtime"], "mode %o" % (localfile["mode"]&0777), "size", localfile["size"]
        # if basefile:
        #    print "base mtime", basefile["localmodtime"], "mode %o" % basefile["mode"], "size", localfile["length"]
        # if serverfile:
        #    serverstat = status[serverfile["fid"]]
        #    print "server mtime", serverstat["modtime"], "mode %o" % serverstat["mode"], "size", serverstat["length"]
    
        delayedtask = synconefile(basepath, f, localfile, serverfile, basefile, parentfid)
        if delayedtask:
            delayedtasks[0:0] = [delayedtask]
        #    break
        #    print
    for (predicate, action) in delayedtasks:
        action()
finally:
    arlalow.giveupallcallbacks(fs.fsconn)

#while 1:
#    print "sleep"
#    arlalow.sleep()
