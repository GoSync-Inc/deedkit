//go:build linux

package main

import (
	"net"
	"syscall"
)

// peerUID is the connecting process's UID from SO_PEERCRED, taken by the kernel at connect.
func peerUID(c *net.UnixConn) (int, error) {
	raw, e := c.SyscallConn()
	if e != nil {
		return -1, e
	}
	uid, failure := -1, error(nil)
	if e = raw.Control(func(fd uintptr) {
		u, err := syscall.GetsockoptUcred(int(fd), syscall.SOL_SOCKET, syscall.SO_PEERCRED)
		if err != nil {
			failure = err
			return
		}
		uid = int(u.Uid)
	}); e != nil {
		return -1, e
	}
	return uid, failure
}
