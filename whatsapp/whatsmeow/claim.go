package main

// ANYM-937: the session holder's ANYM-914 caller. Order for the holder, as for every opener: the canonical host lock,
// the per-directory session locks, then the durable claim row, then the session store. The statements are
// session_claim.py's constants with only the placeholders converted (tests/test_whatsmeow_holder_claim.py compares
// the text and runs both callers on one database). They are Go string constants, never a `.sql` file: a `.sql` path
// is a hand-applied change to the release planner (scripts/production.py SQL_INVENTORY).

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
	"unicode"

	"github.com/jackc/pgx/v5/pgconn"
	_ "github.com/jackc/pgx/v5/stdlib"
)

const claimAcquireSQL = `INSERT INTO session_claims(account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,claimed_at,seen_at)
SELECT $1,$2,$3,$4,$5,$6,1,clock_timestamp(),clock_timestamp()
WHERE EXISTS(SELECT 1 FROM session_claims WHERE account=$1)
   OR NOT EXISTS(SELECT 1 FROM source_status WHERE account=$1 AND status='restored_disabled')
ON CONFLICT(account) DO UPDATE SET host_id=excluded.host_id,boot_id=excluded.boot_id,lock_ref=excluded.lock_ref,
 holder_id=excluded.holder_id,holder_kind=excluded.holder_kind,generation=session_claims.generation+1,
 claimed_at=excluded.claimed_at,seen_at=excluded.seen_at,released_at=NULL,released_by=NULL,
 waiting_host_id=NULL,waiting_since=NULL
WHERE NOT session_claims.restore_hold AND (session_claims.released_at IS NOT NULL OR (
 session_claims.host_id=excluded.host_id AND session_claims.boot_id=excluded.boot_id
 AND session_claims.lock_ref=excluded.lock_ref))
RETURNING generation`
const claimRefreshSQL = `UPDATE session_claims SET seen_at=clock_timestamp() WHERE account=$1 AND generation=$2 AND holder_id=$3
 AND released_at IS NULL AND NOT restore_hold`
const claimReleaseSQL = `UPDATE session_claims SET released_at=clock_timestamp(),released_by='holder'
 WHERE account=$1 AND generation=$2 AND holder_id=$3 AND released_at IS NULL`
const claimWaitingSQL = `UPDATE session_claims SET waiting_host_id=$1,waiting_since=clock_timestamp()
 WHERE account=$2 AND waiting_host_id IS DISTINCT FROM $3`
const claimRowSQL = `SELECT account,host_id,boot_id,lock_ref,holder_id,holder_kind,generation,claimed_at,seen_at,released_at,
 released_by,restore_hold,waiting_host_id,waiting_since FROM session_claims WHERE account=$1`

// The attach check of the collector's write lease (criterion 3): the remaining time is measured by the database's own
// clock, so a clock difference between the holder and the database never extends a lease.
const leaseCheckSQL = `SELECT owner,epoch,expires_at>clock_timestamp() AS live,
 greatest(0,extract(epoch FROM expires_at-clock_timestamp()))::float8 AS remaining
 FROM collector_leases WHERE account=$1`

const (
	claimRefreshEvery = 60 * time.Second
	claimFirstWait    = 5 * time.Second
	claimLongestWait  = 300 * time.Second
	claimLogEvery     = time.Hour
	claimReleaseBound = 5 * time.Second
	claimStatement    = 10 * time.Second
	defaultBootIDPath = "/proc/sys/kernel/random/boot_id"
)

var hex32 = regexp.MustCompile(`^[0-9a-f]{32}$`)

type claimRefused struct{ code string }

func (r claimRefused) Error() string { return r.code }

type hostLock struct {
	file      *os.File
	account   string
	hostID    string
	bootID    string
	lockRef   string
	directory string
}

func (l *hostLock) Close() error { return l.file.Close() }

type sessionClaim struct {
	account    string
	generation int64
	holderID   string
}

func readBootID(path string) (string, error) {
	if path == "" {
		path = defaultBootIDPath
	}
	raw, e := os.ReadFile(path)
	if e != nil {
		return "", errors.New("host_id_invalid")
	}
	value := strings.TrimSpace(string(raw))
	if len(value) < 1 || len(value) > 64 || strings.IndexFunc(value, func(r rune) bool { return !unicode.IsPrint(r) }) >= 0 {
		return "", errors.New("host_id_invalid")
	}
	return value, nil
}

func randomHex(n int) (string, error) {
	b := make([]byte, n)
	if _, e := rand.Read(b); e != nil {
		return "", e
	}
	return hex.EncodeToString(b), nil
}

// readHostID is session_claim.host_id: created once by the first opener, never overwritten.
func readHostID(directory string) (string, error) {
	name := filepath.Join(directory, "host_id")
	fd, e := syscall.Open(name, syscall.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if e == syscall.ENOENT {
		suffix, err := randomHex(4)
		if err != nil {
			return "", err
		}
		temporary := filepath.Join(directory, fmt.Sprintf(".host_id.%d.%s", os.Getpid(), suffix))
		t, err := syscall.Open(temporary, syscall.O_CREAT|syscall.O_EXCL|syscall.O_WRONLY|syscall.O_NOFOLLOW, 0600)
		if err != nil {
			return "", err
		}
		ident, err := randomHex(16)
		if err == nil {
			_, err = syscall.Write(t, []byte(ident+"\n"))
		}
		if err == nil {
			err = syscall.Fsync(t)
		}
		syscall.Close(t)
		if err == nil {
			if le := syscall.Link(temporary, name); le != nil && le != syscall.EEXIST {
				err = le
			}
		}
		_ = syscall.Unlink(temporary)
		if err != nil {
			return "", err
		}
		if d, de := os.Open(directory); de == nil {
			_ = d.Sync()
			d.Close()
		}
		fd, e = syscall.Open(name, syscall.O_RDONLY|syscall.O_NOFOLLOW, 0)
	}
	if e != nil {
		return "", errors.New("host_id_invalid")
	}
	defer syscall.Close(fd)
	var info syscall.Stat_t
	if syscall.Fstat(fd, &info) != nil || info.Mode&syscall.S_IFMT != syscall.S_IFREG || info.Mode&0077 != 0 {
		return "", errors.New("host_id_invalid")
	}
	buf := make([]byte, 64)
	n, _ := syscall.Read(fd, buf)
	if n < 0 {
		n = 0
	}
	value := strings.TrimSpace(string(buf[:n]))
	if !hex32.MatchString(value) {
		return "", errors.New("host_id_invalid")
	}
	return value, nil
}

func lockAccount(account string) error {
	if !regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,64}$`).MatchString(account) || strings.HasPrefix(account, ".") {
		return errors.New("live_account_required")
	}
	return nil
}

// openHostLock holds `<lock directory>/<account>.lock` for the holder's whole lifetime; never unlinked.
func openHostLock(account, directory, bootIDPath string) (*hostLock, error) {
	if e := lockAccount(account); e != nil {
		return nil, e
	}
	if directory == "" {
		directory = defaultSessionLockDirectory
	}
	if e := privateRoot(directory); e != nil {
		return nil, errors.New("private_session_required")
	}
	ident, e := readHostID(directory)
	if e != nil {
		return nil, e
	}
	boot, e := readBootID(bootIDPath)
	if e != nil {
		return nil, e
	}
	f, e := privateFile(directory, account+".lock")
	if e != nil {
		return nil, e
	}
	if syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		f.Close()
		return nil, errors.New("session_already_owned")
	}
	var info syscall.Stat_t
	if e = syscall.Fstat(int(f.Fd()), &info); e != nil {
		f.Close()
		return nil, e
	}
	ref := fmt.Sprintf("%d:%d", uint64(info.Dev), uint64(info.Ino))
	return &hostLock{file: f, account: account, hostID: ident, bootID: boot, lockRef: ref, directory: directory}, nil
}

// sessionDirectoryLock is collector_state.session_lock: `<parent>/<name>.owner.lock`, held for the whole lifetime.
func sessionDirectoryLock(session string) (*os.File, error) {
	parent, name := filepath.Dir(session), filepath.Base(session)
	if e := privateRoot(parent); e != nil {
		return nil, errors.New("private_session_required")
	}
	f, e := privateFile(parent, name+".owner.lock")
	if e != nil {
		return nil, errors.New("private_session_required")
	}
	if syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		f.Close()
		return nil, errors.New("session_already_owned")
	}
	return f, nil
}

func openClaimDatabase(dsnFile string) (*sql.DB, error) {
	if !filepath.IsAbs(dsnFile) {
		return nil, errors.New("holder_dsn_required")
	}
	raw, e := readPrivate(dsnFile, 4096)
	if e != nil {
		return nil, errors.New("holder_dsn_required")
	}
	dsn := strings.TrimSpace(string(raw))
	if dsn == "" {
		return nil, errors.New("holder_dsn_required")
	}
	db, e := sql.Open("pgx", dsn)
	if e != nil {
		return nil, errors.New("holder_dsn_required")
	}
	db.SetMaxOpenConns(2)
	db.SetMaxIdleConns(1)
	db.SetConnMaxIdleTime(5 * time.Minute)
	return db, nil
}

func claimAcquire(ctx context.Context, db *sql.DB, lock *hostLock) (*sessionClaim, error) {
	holder, e := randomHex(16)
	if e != nil {
		return nil, e
	}
	ctx, cancel := context.WithTimeout(ctx, claimStatement)
	defer cancel()
	var generation int64
	e = db.QueryRowContext(ctx, claimAcquireSQL, lock.account, lock.hostID, lock.bootID, lock.lockRef, holder, "holder").Scan(&generation)
	if e == nil {
		return &sessionClaim{account: lock.account, generation: generation, holderID: holder}, nil
	}
	if !errors.Is(e, sql.ErrNoRows) {
		return nil, e
	}
	row, e := claimReadRow(ctx, db, lock.account)
	if e != nil {
		return nil, e
	}
	if row == nil || row.restoreHold {
		return nil, claimRefused{"session_claim_restore_hold"}
	}
	return nil, claimRefused{"session_claimed_elsewhere"}
}

type claimRow struct {
	hostID, holderID, holderKind string
	generation                   int64
	released, restoreHold        bool
}

// claimReadRow runs ROW_SQL; nil when the account has no row.
func claimReadRow(ctx context.Context, db *sql.DB, account string) (*claimRow, error) {
	var r claimRow
	var acct, boot, ref string
	var claimed, seen, released, waitingSince sql.NullTime
	var releasedBy, waitingHost sql.NullString
	e := db.QueryRowContext(ctx, claimRowSQL, account).Scan(&acct, &r.hostID, &boot, &ref, &r.holderID, &r.holderKind,
		&r.generation, &claimed, &seen, &released, &releasedBy, &r.restoreHold, &waitingHost, &waitingSince)
	if errors.Is(e, sql.ErrNoRows) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	r.released = released.Valid
	return &r, nil
}

func claimRefresh(ctx context.Context, db *sql.DB, c *sessionClaim) (bool, error) {
	ctx, cancel := context.WithTimeout(ctx, claimStatement)
	defer cancel()
	r, e := db.ExecContext(ctx, claimRefreshSQL, c.account, c.generation, c.holderID)
	if e != nil {
		return false, e
	}
	n, e := r.RowsAffected()
	return n == 1, e
}

// claimRelease runs only after the verified provider disconnect, bounded for the stop grace; a failure keeps the row
// (the same kernel reacquires it).
func claimRelease(db *sql.DB, c *sessionClaim) bool {
	ctx, cancel := context.WithTimeout(context.Background(), claimReleaseBound)
	defer cancel()
	r, e := db.ExecContext(ctx, claimReleaseSQL, c.account, c.generation, c.holderID)
	if e != nil {
		holderLog("session_claim.release_failed", map[string]any{"account": c.account, "generation": c.generation})
		return false
	}
	n, _ := r.RowsAffected()
	return n == 1
}

func claimMarkWaiting(ctx context.Context, db *sql.DB, lock *hostLock) error {
	ctx, cancel := context.WithTimeout(ctx, claimStatement)
	defer cancel()
	_, e := db.ExecContext(ctx, claimWaitingSQL, lock.hostID, lock.account, lock.hostID)
	return e
}

// backoff is ANYM-914's BACKOFF: 5 s doubling to 5 min, one log line per hour.
type backoff struct {
	delay  time.Duration
	logged time.Time
	now    func() time.Time
	sleep  func(context.Context, time.Duration) error
}

func newBackoff() *backoff {
	return &backoff{delay: claimFirstWait, now: time.Now, sleep: sleepContext}
}

func sleepContext(ctx context.Context, d time.Duration) error {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-t.C:
		return nil
	}
}

func (b *backoff) wait(ctx context.Context, event string, fields map[string]any) error {
	if now := b.now(); b.logged.IsZero() || now.Sub(b.logged) >= claimLogEvery {
		holderLog(event, fields)
		b.logged = now
	}
	e := b.sleep(ctx, b.delay)
	if b.delay *= 2; b.delay > claimLongestWait {
		b.delay = claimLongestWait
	}
	return e
}

// claimAcquireWaiting acquires, or waits in-process holding the locks and no session store open: a refusal or a
// database error never makes provider contact and never exits (no Compose restart loop).
func claimAcquireWaiting(ctx context.Context, db *sql.DB, lock *hostLock, b *backoff) (*sessionClaim, error) {
	marked := false
	for {
		c, e := claimAcquire(ctx, db, lock)
		if e == nil {
			return c, nil
		}
		code, kind := "session_claim_unavailable", databaseErrorKind(e)
		var refused claimRefused
		if errors.As(e, &refused) {
			kind = ""
			code = refused.code
			if !marked && claimMarkWaiting(ctx, db, lock) == nil {
				marked = true
			}
		}
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		fields := map[string]any{"account": lock.account, "code": code}
		if kind != "" {
			fields["error"] = kind
		}
		if e = b.wait(ctx, "session_claim.waiting", fields); e != nil {
			return nil, e
		}
	}
}

// claimKeep refreshes until the row is positively someone else's; a database error is visibility only.
func claimKeep(ctx context.Context, db *sql.DB, c *sessionClaim, every time.Duration) error {
	var logged time.Time
	for {
		if e := sleepContext(ctx, every); e != nil {
			return e
		}
		ours, e := claimRefresh(ctx, db, c)
		if e != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}
			if logged.IsZero() || time.Since(logged) >= claimLogEvery {
				holderLog("session_claim.refresh_failed", map[string]any{"account": c.account})
				logged = time.Now()
			}
			continue
		}
		if !ours {
			holderLog("session_claim.lost", map[string]any{"account": c.account, "generation": c.generation})
			return errors.New("session_claim_lost")
		}
	}
}

// databaseErrorKind names a database failure without its message (which may carry a host or a value): the
// SQLSTATE of a server answer, else timeout or connect.
func databaseErrorKind(e error) string {
	var pg *pgconn.PgError
	switch {
	case errors.As(e, &pg):
		return "sqlstate_" + pg.Code
	case errors.Is(e, context.DeadlineExceeded):
		return "timeout"
	}
	return "connect"
}

type leaseState struct {
	valid     bool
	remaining time.Duration
}

// leaseCheck answers whether (owner, epoch) is the account's live write lease. An error is "unknown", never "invalid".
func leaseCheck(ctx context.Context, db *sql.DB, account, owner string, epoch int64) (leaseState, error) {
	ctx, cancel := context.WithTimeout(ctx, claimStatement)
	defer cancel()
	var rowOwner string
	var rowEpoch int64
	var live bool
	var remaining float64
	e := db.QueryRowContext(ctx, leaseCheckSQL, account).Scan(&rowOwner, &rowEpoch, &live, &remaining)
	if errors.Is(e, sql.ErrNoRows) {
		return leaseState{}, nil
	}
	if e != nil {
		return leaseState{}, e
	}
	if rowOwner != owner || rowEpoch != epoch || !live {
		return leaseState{}, nil
	}
	return leaseState{valid: true, remaining: time.Duration(remaining * float64(time.Second))}, nil
}
