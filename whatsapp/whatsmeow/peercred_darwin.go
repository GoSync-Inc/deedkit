//go:build darwin

package main

import (
	"net"

	"golang.org/x/sys/unix"
)

// peerUID is the connecting process's UID from LOCAL_PEERCRED (development hosts; production runs Linux).
func peerUID(c *net.UnixConn) (int, error) {
	raw, e := c.SyscallConn()
	if e != nil {
		return -1, e
	}
	uid, failure := -1, error(nil)
	if e = raw.Control(func(fd uintptr) {
		x, err := unix.GetsockoptXucred(int(fd), unix.SOL_LOCAL, unix.LOCAL_PEERCRED)
		if err != nil {
			failure = err
			return
		}
		uid = int(x.Uid)
	}); e != nil {
		return -1, e
	}
	return uid, failure
}
