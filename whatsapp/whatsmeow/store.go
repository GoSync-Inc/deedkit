package main

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	waWeb "go.mau.fi/whatsmeow/proto/waWeb"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	"google.golang.org/protobuf/proto"
	"modernc.org/sqlite"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"time"
)

func init() { sql.Register("sqlite3", &sqlite.Driver{}) }

type CaptureStore struct {
	db              *sql.DB
	Config          Config
	Owner           types.JID
	OwnerLID        types.JID
	mu              sync.Mutex
	MaxEntries      int
	MaxSpoolBytes   int64
	MaxSpoolEntries int
}
type savedMessage struct {
	Info   types.MessageInfo
	Wire   []byte
	Web    []byte
	IsEdit bool
	// An edit or flag-only event carries view-once without its wire wrapper;
	// the restriction must survive the cache so a reload never retains media.
	// New records always set it; nil marks a record written before it existed.
	IsViewOnce *bool `json:",omitempty"`
}

func sqliteDSN(path string) string {
	u := url.URL{Scheme: "file", Path: path}
	return u.String() + "?_pragma=foreign_keys(1)&_pragma=busy_timeout(5000)"
}
func OpenCapture(c Config) (*CaptureStore, error) {
	for _, dir := range []string{c.SessionDirectory, c.EventsDirectory} {
		if e := privateRoot(dir); e != nil {
			return nil, e
		}
	}
	f, e := privateFile(c.SessionDirectory, "capture.sqlite")
	if e != nil {
		return nil, e
	}
	f.Close()
	db, e := sql.Open("sqlite3", sqliteDSN(filepath.Join(c.SessionDirectory, "capture.sqlite")))
	if e != nil {
		return nil, e
	}
	db.SetMaxOpenConns(1)
	for _, q := range []string{"PRAGMA journal_mode=DELETE", "PRAGMA synchronous=FULL", "PRAGMA max_page_count=131072", `CREATE TABLE IF NOT EXISTS capture(seq INTEGER PRIMARY KEY,digest TEXT NOT NULL UNIQUE,chat TEXT NOT NULL,message_id TEXT NOT NULL,source_ts INTEGER NOT NULL,raw BLOB NOT NULL,envelope BLOB NOT NULL,published INTEGER NOT NULL DEFAULT 0,created_at INTEGER NOT NULL)`, `CREATE INDEX IF NOT EXISTS capture_chat_time ON capture(chat,source_ts,message_id)`, `CREATE TABLE IF NOT EXISTS chats(chat TEXT PRIMARY KEY,title TEXT NOT NULL,observed_at TEXT NOT NULL)`, `CREATE TABLE IF NOT EXISTS history_requests(id INTEGER PRIMARY KEY CHECK(id=1),last_ms INTEGER NOT NULL)`, `CREATE TABLE IF NOT EXISTS history_jobs(stanza TEXT PRIMARY KEY,wire BLOB NOT NULL,state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,next_attempt_ms INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '')`} {
		if _, e = db.Exec(q); e != nil {
			db.Close()
			return nil, e
		}
	}
	if e = ensureHistoryRetryColumns(db); e != nil {
		db.Close()
		return nil, e
	}
	// The replay worker also runs when no unpublished rows remain. Avoid a
	// repeated scan through the full retained message/media cache in that case.
	if _, e = db.Exec("CREATE INDEX IF NOT EXISTS capture_unpublished ON capture(seq) WHERE published=0"); e != nil {
		db.Close()
		return nil, e
	}
	if _, e = db.Exec(`CREATE TABLE IF NOT EXISTS outgoing_retry(chat TEXT NOT NULL,message_id TEXT NOT NULL,format TEXT NOT NULL,payload BLOB NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(chat,message_id))`); e != nil {
		db.Close()
		return nil, e
	}
	// Content-free counters only: one row per fixed reason code.
	if _, e = db.Exec(`CREATE TABLE IF NOT EXISTS capture_skips(code TEXT PRIMARY KEY,count INTEGER NOT NULL,last_ms INTEGER NOT NULL)`); e != nil {
		db.Close()
		return nil, e
	}
	s := &CaptureStore{db: db, Config: c, MaxEntries: 200000, MaxSpoolBytes: 256 << 20, MaxSpoolEntries: 10000}
	return s, nil
}
func (s *CaptureStore) Close() error { return s.db.Close() }

// Provider data that can never satisfy the archive contract. Such a message is
// acknowledged and counted under its fixed code instead of stopping capture of
// the whole account and being redelivered forever. Store, disk, spool and
// identity failures are never in this set.
var skippedCaptureCodes = map[string]bool{"invalid_provider_message": true, "unsupported_chat": true, "invalid_provider_media": true, "invalid_history_notification": true, "invalid_history_chat": true, "invalid_history_message": true}

func skippableCapture(err error) bool { return err != nil && skippedCaptureCodes[err.Error()] }

// skip records only the reason code; no message content, ID or chat is kept.
func (s *CaptureStore) skip(err error) error {
	code := err.Error()
	if !skippedCaptureCodes[code] {
		return err
	}
	if _, e := s.db.Exec("INSERT INTO capture_skips(code,count,last_ms)VALUES(?,1,?)ON CONFLICT(code)DO UPDATE SET count=count+1,last_ms=excluded.last_ms", code, time.Now().UnixMilli()); e != nil {
		return e
	}
	_ = json.NewEncoder(os.Stderr).Encode(map[string]any{"state": "capture_skipped", "reason": code})
	return nil
}

// putOrSkip durably captures a message or counts a permanently invalid one.
func (s *CaptureStore) putOrSkip(e *events.Message, origin string) error {
	err := s.Put(e, origin)
	if skippableCapture(err) {
		return s.skip(err)
	}
	return err
}

const maxOutgoingRetryBytes = 32 << 20
const maxOutgoingRetryMessages = 1000
const maxCaptureEnvelopeBytes = 512 << 10
const replayBatchEntries = 32
const replayBatchBytes = 16 << 20

// Decryption transactions remain in whatsmeow's session store. Exact outgoing
// wire payloads use our bounded durable cache instead of an unbounded second
// archive. Protocol retry preserves the original stanza ID and is separately
// fenced by the Provider's current identity, readiness, and policy.
type nativeEventBuffer struct {
	store.EventBuffer
	Capture *CaptureStore
}

func (b *nativeEventBuffer) AddOutgoingEvent(ctx context.Context, chat types.JID, id types.MessageID, format string, payload []byte) error {
	if len(payload) == 0 || len(payload) > 512<<10 || (format != "wa" && format != "fb") {
		return errors.New("capture_overload")
	}
	b.Capture.mu.Lock()
	defer b.Capture.mu.Unlock()
	if err := b.Capture.checkDisk(int64(len(payload))); err != nil {
		return err
	}
	tx, err := b.Capture.db.BeginTx(ctx, nil)
	if err != nil {
		return err
	}
	defer tx.Rollback()
	if _, err = tx.ExecContext(ctx, "DELETE FROM outgoing_retry WHERE created_at<?", time.Now().Add(-7*24*time.Hour).UnixMilli()); err != nil {
		return err
	}
	if _, err = tx.ExecContext(ctx, "INSERT INTO outgoing_retry(chat,message_id,format,payload,created_at)VALUES(?,?,?,?,?)ON CONFLICT(chat,message_id)DO UPDATE SET format=excluded.format,payload=excluded.payload,created_at=excluded.created_at", chat.ToNonAD().String(), id, format, payload, time.Now().UnixMilli()); err != nil {
		return err
	}
	for {
		var count, size int64
		if err = tx.QueryRowContext(ctx, "SELECT COUNT(*),COALESCE(SUM(length(payload)),0)FROM outgoing_retry").Scan(&count, &size); err != nil {
			return err
		}
		if count <= maxOutgoingRetryMessages && size <= maxOutgoingRetryBytes {
			break
		}
		if _, err = tx.ExecContext(ctx, "DELETE FROM outgoing_retry WHERE rowid IN(SELECT rowid FROM outgoing_retry ORDER BY created_at,rowid LIMIT 1)"); err != nil {
			return err
		}
	}
	return tx.Commit()
}

func (b *nativeEventBuffer) GetOutgoingEvent(ctx context.Context, chat, alt types.JID, id types.MessageID) (format string, payload []byte, err error) {
	err = b.Capture.db.QueryRowContext(ctx, "SELECT format,payload FROM outgoing_retry WHERE (chat=? OR chat=?)AND message_id=?AND created_at>=? ORDER BY created_at DESC LIMIT 1", chat.ToNonAD().String(), alt.ToNonAD().String(), id, time.Now().Add(-7*24*time.Hour).UnixMilli()).Scan(&format, &payload)
	return
}

func (b *nativeEventBuffer) DeleteOldOutgoingEvents(ctx context.Context) error {
	_, err := b.Capture.db.ExecContext(ctx, "DELETE FROM outgoing_retry WHERE created_at<?", time.Now().Add(-7*24*time.Hour).UnixMilli())
	return err
}
func marshalMessage(e *events.Message) ([]byte, error) {
	raw := e.RawMessage
	if raw == nil {
		raw = e.Message
	}
	var wire, web []byte
	var err error
	if raw != nil {
		wire, err = proto.Marshal(raw)
		if err != nil {
			return nil, err
		}
	}
	if e.SourceWebMsg != nil {
		web, err = proto.Marshal(e.SourceWebMsg)
		if err != nil {
			return nil, err
		}
	}
	viewOnce := e.IsViewOnce
	return json.Marshal(savedMessage{Info: e.Info, Wire: wire, Web: web, IsEdit: e.IsEdit, IsViewOnce: &viewOnce})
}
func unmarshalMessage(b []byte) (*events.Message, error) {
	var saved savedMessage
	if e := json.Unmarshal(b, &saved); e != nil {
		return nil, e
	}
	msg := &events.Message{Info: saved.Info, IsEdit: saved.IsEdit}
	if len(saved.Wire) > 0 {
		msg.RawMessage = &waE2E.Message{}
		if e := proto.Unmarshal(saved.Wire, msg.RawMessage); e != nil {
			return nil, e
		}
		msg.UnwrapRaw()
		unwrapAlbumChild(msg)
	}
	if saved.IsViewOnce != nil {
		msg.IsViewOnce = msg.IsViewOnce || *saved.IsViewOnce
	} else if msg.Message.GetPtvMessage() != nil {
		// An older record cannot tell a view-once round video from an ordinary
		// one; it keeps the attachment-free projection it was captured with.
		msg.Message.PtvMessage = nil
	}
	if len(saved.Web) > 0 {
		msg.SourceWebMsg = &waWeb.WebMessageInfo{}
		if e := proto.Unmarshal(saved.Web, msg.SourceWebMsg); e != nil {
			return nil, e
		}
	}
	return msg, nil
}
func (s *CaptureStore) Put(e *events.Message, origin string) error {
	if e == nil {
		return errors.New("invalid_provider_message")
	}
	if origin != "live" && origin != "history" {
		return errors.New("invalid_capture_origin")
	}
	if _, err := ParseChatID(LegacyChatID(e.Info.Chat)); err != nil {
		return nil
	} // supported sources only
	e = unwrap(e)
	if controlOnly(e.Message) {
		return nil
	}
	if pm := e.Message.GetProtocolMessage(); pm != nil && pm.GetType() == waE2E.ProtocolMessage_MESSAGE_EDIT && pm.GetEditedMessage() != nil {
		copy := *e
		copy.Info.ID = pm.GetKey().GetID()
		copy.Info.IsFromMe = pm.GetKey().GetFromMe()
		copy.IsEdit = true
		copy.Info.MsgBotInfo.EditSenderTimestampMS = time.UnixMilli(pm.GetTimestampMS())
		copy.Message = pm.GetEditedMessage()
		copy.RawMessage = copy.Message
		if old, err := s.Get(LegacyChatID(copy.Info.Chat), LegacyMessageID(&copy)); err == nil {
			copy.Info.Timestamp = old.Info.Timestamp
			copy.Info.Sender = old.Info.Sender
		}
		e = &copy
	}
	envelope, err := captureEnvelope(s.Config.Account, e, s.Owner, origin, s.OwnerLID)
	if err != nil {
		return err
	}
	raw, err := marshalMessage(e)
	if err != nil {
		return err
	}
	id := LegacyMessageID(e)
	if envelope["kind"] == "whatsapp_delete" {
		id = envelope["ids"].([]string)[0]
		raw = nil
	}
	return s.append(LegacyChatID(e.Info.Chat), id, e.Info.Timestamp.Unix(), raw, envelope, origin == "history")
}

// deferExport (history backfill): a durably journaled row that cannot be
// exported now is left to the replay worker instead of failing the batch. A
// retried batch therefore skips rows it already stored and keeps going.
func (s *CaptureStore) append(chat, id string, stamp int64, raw []byte, envelope map[string]any, deferExport bool) error {
	if raw == nil {
		raw = []byte{}
	}
	data, err := json.Marshal(envelope)
	if err != nil {
		return err
	}
	if len(data) > maxCaptureEnvelopeBytes {
		return errors.New("capture_too_large")
	}
	if len(data)+len(raw) > maxCaptureEnvelopeBytes {
		// The bounded archive envelope always fits; oversized native evidence
		// (e.g. a long quoted message) is not retained locally. The capture is
		// still exported, but cannot later serve pages, media or edit lookups.
		raw = []byte{}
	}
	// Observation time/origin do not turn a history replay into a new revision.
	canonical := map[string]any{}
	for k, v := range envelope {
		if k != "observed_at" && k != "origin" {
			canonical[k] = v
		}
	}
	if v, ok := canonical["event"].(map[string]any); ok {
		copy := map[string]any{}
		for k, x := range v {
			if k != "identityObservations" {
				copy[k] = x
			}
		}
		canonical["event"] = copy
	}
	stable, _ := json.Marshal(canonical)
	h := sha256.New()
	h.Write(stable)
	h.Write([]byte{0})
	h.Write(raw)
	digest := hex.EncodeToString(h.Sum(nil))
	s.mu.Lock()
	defer s.mu.Unlock()
	var published int
	var storedData []byte
	err = s.db.QueryRow("SELECT published,envelope FROM capture WHERE digest=?", digest).Scan(&published, &storedData)
	if err == nil {
		if published == 1 || deferExport {
			return nil
		}
		return s.publish(digest, storedData)
	}
	if !errors.Is(err, sql.ErrNoRows) {
		return err
	}
	var count int
	if err = s.db.QueryRow("SELECT COUNT(*) FROM capture").Scan(&count); err != nil {
		return err
	}
	if count >= s.MaxEntries {
		// Only already consumed, published archive copies may leave the bounded
		// local history/media cache. A pending spool record is never discarded.
		rows, e := s.db.Query("SELECT seq,digest,envelope FROM capture WHERE published=1 ORDER BY seq LIMIT 1000")
		if e != nil {
			return e
		}
		var drop []int64
		for rows.Next() {
			var seq int64
			var d string
			var payload []byte
			if e = rows.Scan(&seq, &d, &payload); e != nil {
				rows.Close()
				return e
			}
			consumed := true
			// Pre-fix exports used the semantic capture key in their filename.
			// Preserve those journal rows until the operator verifies and repairs
			// their exports; either pending filename still protects the capture.
			for _, name := range []string{captureSpoolFilename(seq, payload), fmt.Sprintf("%020d-%s.json", seq, d)} {
				if _, e = os.Lstat(filepath.Join(s.Config.EventsDirectory, name)); !errors.Is(e, os.ErrNotExist) {
					consumed = false
					break
				}
			}
			if consumed {
				drop = append(drop, seq)
			}
		}
		if e = rows.Err(); e != nil {
			rows.Close()
			return e
		}
		rows.Close()
		for _, seq := range drop {
			if _, err = s.db.Exec("DELETE FROM capture WHERE seq=? AND published=1", seq); err != nil {
				return err
			}
			count--
		}
		if count >= s.MaxEntries {
			return errors.New("capture_overload")
		}
	}
	if err = s.checkDisk(int64(len(data) + len(raw))); err != nil {
		return err
	}
	if _, err = s.db.Exec("INSERT INTO capture(digest,chat,message_id,source_ts,raw,envelope,created_at) VALUES(?,?,?,?,?,?,?)", digest, chat, id, stamp, raw, data, time.Now().Unix()); err != nil {
		return err
	}
	if chat != "" {
		if _, err = s.db.Exec("INSERT INTO chats(chat,title,observed_at)VALUES(?,?,?) ON CONFLICT(chat) DO UPDATE SET observed_at=excluded.observed_at", chat, chat, time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
			return err
		}
	}
	err = s.publish(digest, data)
	if deferExport && spoolBackpressure(err) {
		return nil
	}
	return err
}
func (s *CaptureStore) checkDisk(extra int64) error {
	var stat syscall.Statfs_t
	if e := syscall.Statfs(s.Config.EventsDirectory, &stat); e != nil {
		return e
	}
	if int64(stat.Bavail)*int64(stat.Bsize)-extra < s.Config.DiskReserveBytes {
		return errors.New("disk_reserve")
	}
	return nil
}

type spoolUsage struct {
	count int
	bytes int64
}

func measureSpool(entries []os.DirEntry) (spoolUsage, error) {
	var usage spoolUsage
	for _, entry := range entries {
		if entry.Type()&os.ModeSymlink != 0 {
			return usage, errors.New("spool_symlink")
		}
		info, e := entry.Info()
		if errors.Is(e, os.ErrNotExist) {
			// The archive consumer removes an event only after committing it.
			// Its concurrent drain is expected and simply frees capacity.
			continue
		}
		if e != nil {
			return usage, e
		}
		if info.Mode().IsRegular() {
			usage.bytes += info.Size()
			if strings.HasSuffix(entry.Name(), ".json") {
				usage.count++
			}
		}
	}
	return usage, nil
}

func (s *CaptureStore) spoolUsage() (spoolUsage, error) {
	entries, e := os.ReadDir(s.Config.EventsDirectory)
	if e != nil {
		return spoolUsage{}, e
	}
	return measureSpool(entries)
}

// The semantic capture key intentionally ignores observation time/origin and
// includes the native raw message. The Python consumer's integrity suffix must
// instead hash the exact serialized envelope bytes that it will read.
func captureSpoolFilename(seq int64, payload []byte) string {
	return fmt.Sprintf("%020d-%x.json", seq, sha256.Sum256(payload))
}

func (s *CaptureStore) publish(digest string, data []byte) error {
	usage, e := s.spoolUsage()
	if e != nil {
		return e
	}
	return s.publishWithUsage(digest, data, &usage)
}

// Callers hold mu, so only the archive consumer can change the directory while
// this budget is in use. Concurrent removals only make our estimate conservative.
func (s *CaptureStore) publishWithUsage(digest string, data []byte, usage *spoolUsage) error {
	var seq int64
	if e := s.db.QueryRow("SELECT seq FROM capture WHERE digest=?", digest).Scan(&seq); e != nil {
		return e
	}
	name := captureSpoolFilename(seq, data)
	target := filepath.Join(s.Config.EventsDirectory, name)
	if old, e := readPrivate(target, 512<<10); e == nil {
		if string(old) != string(data) {
			return errors.New("capture_digest_conflict")
		}
	} else if !errors.Is(e, os.ErrNotExist) {
		return e
	} else {
		if usage.bytes+int64(len(data)) > s.MaxSpoolBytes || usage.count >= s.MaxSpoolEntries {
			return errors.New("spool_full")
		}
		if e = s.checkDisk(int64(len(data))); e != nil {
			return e
		}
		if e = atomicPrivate(s.Config.EventsDirectory, name, data); e != nil {
			return e
		}
		usage.count++
		usage.bytes += int64(len(data))
	}
	_, e := s.db.Exec("UPDATE capture SET published=1 WHERE digest=?", digest)
	return e
}
func (s *CaptureStore) Replay() error {
	s.mu.Lock()
	defer s.mu.Unlock()
	// Bound both RAM and mutex hold time. The lifecycle worker drains subsequent
	// oldest-first batches; startup never materializes the entire capture cache.
	rows, e := s.db.Query("SELECT digest,envelope FROM capture WHERE published=0 ORDER BY seq LIMIT ?", replayBatchEntries)
	if e != nil {
		return e
	}
	type item struct {
		d string
		b []byte
	}
	var pending []item
	var size int
	for rows.Next() {
		var i item
		if e = rows.Scan(&i.d, &i.b); e != nil {
			rows.Close()
			return e
		}
		if len(i.b) > maxCaptureEnvelopeBytes {
			rows.Close()
			return errors.New("capture_too_large")
		}
		if size+len(i.b) > replayBatchBytes {
			break
		}
		size += len(i.b)
		pending = append(pending, i)
	}
	if e = rows.Err(); e != nil {
		rows.Close()
		return e
	}
	rows.Close()
	if len(pending) == 0 {
		return nil
	}
	usage, e := s.spoolUsage()
	if e != nil {
		return e
	}
	for _, i := range pending {
		if e = s.publishWithUsage(i.d, i.b, &usage); e != nil {
			return e
		}
	}
	return nil
}
func atomicPrivate(dir, name string, data []byte) error {
	var entropy [12]byte
	if _, e := rand.Read(entropy[:]); e != nil {
		return e
	}
	tmp := ".pending-" + hex.EncodeToString(entropy[:])
	f, e := privateFile(dir, tmp)
	if e != nil {
		return e
	}
	defer os.Remove(filepath.Join(dir, tmp))
	if _, e = f.Write(data); e == nil {
		e = f.Sync()
	}
	ce := f.Close()
	if e != nil {
		return e
	}
	if ce != nil {
		return ce
	}
	if e = os.Rename(filepath.Join(dir, tmp), filepath.Join(dir, name)); e != nil {
		return e
	}
	d, e := os.Open(dir)
	if e != nil {
		return e
	}
	defer d.Close()
	return d.Sync()
}
func (s *CaptureStore) Get(chat, id string) (*events.Message, error) {
	var raw []byte
	e := s.db.QueryRow("SELECT raw FROM capture WHERE chat=? AND message_id=? ORDER BY seq DESC LIMIT 1", chat, id).Scan(&raw)
	if e != nil || len(raw) == 0 {
		return nil, errors.New("message_target_unavailable")
	}
	return unmarshalMessage(raw)
}
func (s *CaptureStore) Page(chat string, limit int, before []any) ([]*events.Message, error) {
	if limit < 1 || limit > 100 {
		return nil, errors.New("invalid_limit")
	}
	query := "SELECT raw FROM capture WHERE seq IN (SELECT MAX(seq) FROM capture WHERE chat=? GROUP BY message_id) AND length(raw)>0"
	args := []any{chat}
	if before != nil {
		if len(before) != 2 {
			return nil, errors.New("invalid_cursor")
		}
		stamp, ok := before[0].(float64)
		id, ok2 := before[1].(string)
		if !ok || !ok2 || stamp <= 0 || stamp != float64(int64(stamp)) {
			return nil, errors.New("invalid_cursor")
		}
		query += " AND (source_ts<? OR (source_ts=? AND message_id<?))"
		args = append(args, int64(stamp), int64(stamp), id)
	}
	query += " ORDER BY source_ts DESC,message_id DESC LIMIT ?"
	args = append(args, limit)
	rows, e := s.db.Query(query, args...)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []*events.Message{}
	for rows.Next() {
		var raw []byte
		if e = rows.Scan(&raw); e != nil {
			return nil, e
		}
		m, e := unmarshalMessage(raw)
		if e != nil {
			return nil, e
		}
		out = append(out, m)
	}
	for i, j := 0, len(out)-1; i < j; i, j = i+1, j-1 {
		out[i], out[j] = out[j], out[i]
	}
	return out, rows.Err()
}
func (s *CaptureStore) Latest(chat string) (*events.Message, error) {
	v, e := s.Page(chat, 1, nil)
	if e != nil {
		return nil, e
	}
	if len(v) == 0 {
		return nil, sql.ErrNoRows
	}
	return v[0], nil
}
func (s *CaptureStore) Dialogs(limit int, policy Policy) ([]any, error) {
	if limit < 1 || limit > 10000 {
		return nil, errors.New("invalid_limit")
	}
	rows, e := s.db.Query("SELECT chat,title,observed_at FROM chats ORDER BY observed_at DESC LIMIT ?", 10000)
	if e != nil {
		return nil, e
	}
	defer rows.Close()
	out := []any{}
	for rows.Next() {
		var chat, title, at string
		if e = rows.Scan(&chat, &title, &at); e != nil {
			return nil, e
		}
		if !policy.Allows(chat) {
			continue
		}
		group := strings.HasSuffix(chat, "@g.us")
		kind, typ := "user", "private"
		var self any
		if group {
			kind = "chat"
			typ = "group"
			self = false
		} else if j, e := ParseChatID(chat); e == nil && j.Server == s.Owner.Server {
			self = j == s.Owner.ToNonAD()
		} else if e == nil && j.Server == types.HiddenUserServer && !s.OwnerLID.IsEmpty() {
			// The owner's own chat may be addressed by LID instead of phone number.
			self = j == s.OwnerLID.ToNonAD()
		}
		out = append(out, []any{chat, kind, title, typ, map[string]any{"observed_at": at, "unread_count": nil, "basis": "whatsmeow_captured_chat", "is_self_chat": self}})
		if len(out) >= limit {
			break
		}
	}
	return out, rows.Err()
}
func (s *CaptureStore) StoreReceipt(e *events.Receipt) error {
	state := map[types.ReceiptType]string{types.ReceiptTypeDelivered: "delivered", types.ReceiptTypeRead: "read", types.ReceiptTypePlayed: "played"}[e.Type]
	if state == "" {
		return nil
	}
	chat := LegacyChatID(e.Chat)
	audience := "private_peer"
	if e.IsGroup {
		audience = "provider_aggregate"
	}
	for _, id := range e.MessageIDs {
		// Receipts can reference incoming messages on sibling devices. Only an
		// already captured outgoing ID may become outgoing delivery evidence.
		rows, err := s.db.Query("SELECT message_id,raw FROM capture WHERE chat=? AND length(raw)>0 ORDER BY seq DESC LIMIT 1000", chat)
		if err != nil {
			return err
		}
		var found string
		for rows.Next() {
			var candidate string
			var raw []byte
			if err = rows.Scan(&candidate, &raw); err != nil {
				rows.Close()
				return err
			}
			m, err := unmarshalMessage(raw)
			if err == nil && m.Info.ID == id && m.Info.IsFromMe {
				found = candidate
				break
			}
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		if found == "" {
			continue
		}
		value := map[string]any{"kind": "message_receipt", "account": s.Config.Account, "platform": "whatsapp", "chat": chat, "provider_id": found, "max_id": nil, "state": state, "basis": "whatsapp_message_ack", "audience": audience, "observed_at": time.Now().UTC().Format(time.RFC3339Nano), "provider_at": nil}
		if err = s.append(chat, fmt.Sprintf("receipt:%s:%s", found, state), e.Timestamp.Unix(), nil, value, false); err != nil {
			return err
		}
	}
	return nil
}

// ReceiptObservations returns only receipt evidence actually retained by this
// device. Missing rows do not imply an unread or undelivered message, and group
// receipts remain aggregate evidence without a fabricated participant list.
func (s *CaptureStore) ReceiptObservations(chat, id string) ([]map[string]any, error) {
	out := []map[string]any{}
	for _, state := range []string{"delivered", "read", "played"} {
		var data []byte
		err := s.db.QueryRow("SELECT envelope FROM capture WHERE chat=? AND message_id=? ORDER BY seq DESC LIMIT 1", chat, "receipt:"+id+":"+state).Scan(&data)
		if errors.Is(err, sql.ErrNoRows) {
			continue
		}
		if err != nil {
			return nil, err
		}
		var observation map[string]any
		if err = json.Unmarshal(data, &observation); err != nil {
			return nil, err
		}
		out = append(out, observation)
	}
	return out, nil
}
