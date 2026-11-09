package main

// ANYM-937: the holder's sockets and handshake (COLLECTORS.md, Session holders): the peer UID and token, the
// protocol range, the lease-verified attachment, keepalives and the two lanes. serve.go holds the provider side.

import (
	"bufio"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"net"
	"os"
	"path/filepath"
	"strings"
	"time"
)

type collectorHello struct {
	Hello struct {
		Protocol  int    `json:"protocol"`
		Supported [2]int `json:"supported"`
	} `json:"hello"`
	Token string   `json:"token"`
	Lane  string   `json:"lane"`
	Lease leaseRef `json:"lease"`
}

// negotiate picks the protocol both sides support, or names the side that is outside the other's range.
func negotiate(holder, collector [2]int) (int, string) {
	if collector[0] < 1 || collector[1] < collector[0] {
		return 0, "collector_protocol_unsupported"
	}
	if collector[1] < holder[0] {
		return 0, "collector_protocol_unsupported"
	}
	if holder[1] < collector[0] {
		return 0, "holder_protocol_unsupported"
	}
	if holder[1] < collector[1] {
		return holder[1], ""
	}
	return collector[1], ""
}

func readFrame(r *bufio.Reader) ([]byte, error) {
	var line []byte
	for {
		chunk, prefix, e := r.ReadLine()
		if e != nil {
			return nil, e
		}
		line = append(line, chunk...)
		if len(line) > maxFrame {
			return nil, errors.New("provider_request_too_large")
		}
		if !prefix {
			return line, nil
		}
	}
}

// accept runs the handshake of one connection: peer UID, the holder's hello, the collector's hello with the token, the
// protocol range and the lease. Nothing else runs before it passes.
func (h *holder) accept(conn *net.UnixConn, lane string) {
	fc := &frameConn{conn: conn}
	refuse := func(code string) {
		_ = fc.write(map[string]any{"error": code})
		conn.Close()
	}
	if uid, e := peerUID(conn); e != nil || uid != os.Getuid() {
		refuse("holder_peer_refused")
		return
	}
	_ = conn.SetDeadline(time.Now().Add(handshakeTimeout))
	h.mu.Lock()
	supported := h.supported
	h.mu.Unlock()
	if fc.write(map[string]any{"hello": map[string]any{"protocol": holderProtocol, "supported": supported,
		"holder_source_sha": sourceSHA, "account": h.cfg.Account, "methods": h.methods(), "lane": lane}}) != nil {
		conn.Close()
		return
	}
	reader := bufio.NewReaderSize(conn, 64<<10)
	raw, e := readFrame(reader)
	if e != nil {
		conn.Close()
		return
	}
	var hello collectorHello
	if json.Unmarshal(raw, &hello) != nil {
		refuse("holder_token_invalid")
		return
	}
	if len(hello.Token) != len(h.token) || subtle.ConstantTimeCompare([]byte(hello.Token), []byte(h.token)) != 1 {
		refuse("holder_token_invalid")
		return
	}
	protocol, code := negotiate(supported, hello.Hello.Supported)
	if code != "" {
		refuse(code)
		return
	}
	if hello.Lane != lane || hello.Lease.Owner == "" || hello.Lease.Epoch < 1 {
		refuse("collector_lease_invalid")
		return
	}
	// Attaching may wait for a predecessor's admitted calls to return; the token was already checked.
	_ = conn.SetDeadline(time.Time{})
	att, code := h.attach(lane, fc, hello.Lease)
	if code != "" {
		refuse(code)
		return
	}
	_ = conn.SetDeadline(time.Time{})
	if e = fc.write(map[string]any{"attached": h.attachedReply(protocol, lane)}); e != nil {
		h.detach(att, "collector_gone")
		return
	}
	h.serveLane(att, lane, fc, reader)
}

func (h *holder) attachedReply(protocol int, lane string) map[string]any {
	h.mu.Lock()
	defer h.mu.Unlock()
	return map[string]any{"protocol": protocol, "lane": lane, "account": h.cfg.Account, "generation": h.provGen,
		"connected": h.prov != nil && h.prov.isReady(), "terminal": h.terminal}
}

// attach: the control lane makes the attachment after a positive database answer for its lease; the send lane joins
// the attachment that holds the same lease. A database that cannot answer never attaches.
func (h *holder) attach(lane string, fc *frameConn, lease leaseRef) (*attachment, string) {
	if lane == "send" {
		h.mu.Lock()
		defer h.mu.Unlock()
		att := h.att
		if att == nil || att.lease != lease {
			return nil, "holder_not_attached"
		}
		if att.send != nil {
			return nil, "holder_busy"
		}
		att.send = fc
		return att, ""
	}
	valid, deadline, e := h.verifyLease(nil, lease)
	h.mu.Lock()
	current := h.att
	h.mu.Unlock()
	if e != nil {
		if current != nil {
			return nil, "holder_busy"
		}
		return nil, "collector_lease_unverified"
	}
	if !valid {
		return nil, "collector_lease_invalid"
	}
	if current != nil {
		if current.lease == lease {
			return nil, "holder_busy" // a second live attachment of one lease
		}
		// The database names the new lease the account's live one: the old one is positively invalid. Its admitted
		// calls finish before the successor is attached, so two collectors never act at once.
		h.detach(current, "collector_lease_invalid")
		current.inflight.Wait()
	}
	att := &attachment{lease: lease, control: fc, verifiedUntil: deadline, attachedAt: h.now(), closed: make(chan struct{})}
	h.mu.Lock()
	if h.att != nil {
		h.mu.Unlock()
		return nil, "holder_busy"
	}
	h.att = att
	generation := h.provGen
	h.mu.Unlock()
	holderLog("holder.attached", map[string]any{"account": h.cfg.Account, "epoch": lease.Epoch, "generation": generation})
	return att, ""
}

// detach closes both lanes of the attachment once. In-flight handlers keep running on the holder's context; their
// verified outcome stays in the journals (send receipts, operation outcomes).
func (h *holder) detach(att *attachment, reason string) {
	att.once.Do(func() {
		h.mu.Lock()
		att.dead = true
		if h.att == att {
			h.att = nil
		}
		send := att.send
		h.mu.Unlock()
		att.control.conn.Close()
		if send != nil {
			send.conn.Close()
		}
		close(att.closed)
		holderLog("holder.detached", map[string]any{"account": h.cfg.Account, "epoch": att.lease.Epoch, "reason": reason,
			"attached_s": int64(h.now().Sub(att.attachedAt).Seconds())})
	})
}

func (h *holder) attached(att *attachment) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.att == att
}

type keepaliveFrame struct {
	Keepalive *struct {
		Lease  leaseRef        `json:"lease"`
		Policy json.RawMessage `json:"policy"`
	} `json:"keepalive"`
}

// keepalive renews the attachment (a database answer on each one) and then applies the policy it carries. Only a
// positive "not this lease" detaches; while the database cannot answer, the last verified lease holds until it ends.
func (h *holder) keepalive(att *attachment, k keepaliveFrame) {
	if k.Keepalive.Lease != att.lease {
		h.detach(att, "collector_lease_invalid")
		return
	}
	if valid, _, e := h.verifyLease(att, att.lease); e == nil && !valid {
		h.detach(att, "collector_lease_invalid")
		return
	}
	h.mu.Lock()
	live := h.now().Before(att.verifiedUntil)
	h.mu.Unlock()
	if !live {
		h.detach(att, "collector_lease_invalid")
		return
	}
	if len(k.Keepalive.Policy) > 0 {
		_ = h.applyPolicyFor(att, k.Keepalive.Policy)
	}
}

// serveLane reads frames: a keepalive (control lane) is applied at once, so a long media answer never delays an
// exclusion or the lease; requests run one at a time, in order, as in child mode's runLane.
func (h *holder) serveLane(att *attachment, lane string, fc *frameConn, reader *bufio.Reader) {
	allowed := map[string]bool{}
	for _, m := range controlMethods {
		allowed[m] = lane == "control"
	}
	for _, m := range sendMethods {
		allowed[m] = lane == "send"
	}
	requests := make(chan request, 64)
	done := make(chan struct{})
	go func() {
		defer close(done)
		for r := range requests {
			if !h.answer(att, lane, fc, r) {
				h.detach(att, "collector_gone")
				for range requests {
				}
				return
			}
		}
	}()
	defer func() {
		close(requests)
		<-done
	}()
	for {
		raw, e := readFrame(reader)
		if e != nil {
			h.detach(att, "collector_gone")
			return
		}
		if lane == "control" && strings.HasPrefix(strings.TrimLeft(string(raw), " "), `{"keepalive"`) {
			var k keepaliveFrame
			if json.Unmarshal(raw, &k) != nil || k.Keepalive == nil {
				h.detach(att, "collector_protocol_error")
				return
			}
			h.keepalive(att, k)
			continue
		}
		var r request
		if json.Unmarshal(raw, &r) != nil || r.ID < 1 || r.ID > 9007199254740991 {
			if fc.write(map[string]any{"id": r.ID, "error": "provider_operation_failed"}) != nil {
				h.detach(att, "collector_gone")
				return
			}
			continue
		}
		if !allowed[r.Method] {
			// A method the hello did not list is never called (criterion 2).
			if fc.write(map[string]any{"id": r.ID, "error": "operation_unsupported"}) != nil {
				h.detach(att, "collector_gone")
				return
			}
			continue
		}
		select {
		case requests <- r:
		case <-att.closed:
			return
		}
	}
}

var errLeaseRefused = errors.New("collector_lease_invalid")

// answer runs one request and writes its reply; false when the lane can no longer be written.
func (h *holder) answer(att *attachment, lane string, fc *frameConn, r request) bool {
	emit := func(v any) error { return fc.write(v) }
	result, e := h.dispatch(att, lane, r, emit)
	if e == errLeaseRefused {
		_ = fc.write(map[string]any{"id": r.ID, "error": e.Error()})
		h.detach(att, "collector_lease_invalid")
		return false
	}
	if e != nil {
		return fc.write(map[string]any{"id": r.ID, "error": safeError(e)}) == nil
	}
	if e = fc.write(map[string]any{"id": r.ID, "result": result}); e != nil {
		if e.Error() != "provider_response_too_large" || r.Method == "send_text" || r.Method == "messenger_operation" {
			return false
		}
		return fc.write(map[string]any{"id": r.ID, "error": "provider_response_too_large"}) == nil
	}
	return true
}

func listenPrivate(directory, name string) (*net.UnixListener, error) {
	path := filepath.Join(directory, name)
	// Called only under the session flocks: a stale socket of a previous holder is removed, nothing else is.
	if info, e := os.Lstat(path); e == nil {
		if info.Mode()&os.ModeSocket == 0 {
			return nil, errors.New("holder_socket_path_invalid")
		}
		if e = os.Remove(path); e != nil {
			return nil, e
		}
	}
	l, e := net.ListenUnix("unix", &net.UnixAddr{Name: path, Net: "unix"})
	if e != nil {
		return nil, e
	}
	l.SetUnlinkOnClose(true)
	if e = os.Chmod(path, 0600); e != nil {
		l.Close()
		return nil, e
	}
	return l, nil
}

// listen writes a fresh token (0600) and opens both sockets (0600) in the 0700 socket directory.
func (h *holder) listen() error {
	token, e := randomHex(32)
	if e != nil {
		return e
	}
	h.token = token
	if e = atomicPrivate(h.socketDir, holderTokenFile, []byte(token+"\n")); e != nil {
		return e
	}
	for _, lane := range []string{"control", "send"} {
		name := controlSocket
		if lane == "send" {
			name = sendSocket
		}
		l, e := listenPrivate(h.socketDir, name)
		if e != nil {
			h.closeListeners()
			return e
		}
		h.listeners = append(h.listeners, l)
		go h.acceptLoop(l, lane)
	}
	return nil
}

func (h *holder) acceptLoop(l *net.UnixListener, lane string) {
	pending := make(chan struct{}, 4)
	for {
		conn, e := l.AcceptUnix()
		if e != nil {
			if errors.Is(e, net.ErrClosed) {
				return
			}
			time.Sleep(100 * time.Millisecond)
			continue
		}
		select {
		case pending <- struct{}{}:
		default:
			conn.Close() // more handshakes at once than any one collector makes
			continue
		}
		go func() {
			defer func() { <-pending }()
			h.accept(conn, lane)
		}()
	}
}

func (h *holder) closeListeners() {
	for _, l := range h.listeners {
		l.Close()
	}
	h.listeners = nil
}
