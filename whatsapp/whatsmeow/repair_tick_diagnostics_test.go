package main

// Where and why a repair pass refused to store (DK-91 follow-up): a repair_storage error now carries a closed phase
// and reason from the tick that produced it, never the underlying error text. Synthetic rows in a temp SQLite file;
// each refusal is triggered through the real pass, and the ordinary repair_storage and fail-closed outcome stay.

import (
	"context"
	"database/sql"
	"database/sql/driver"
	"path/filepath"
	"strings"
	"syscall"
	"testing"
	"time"
)

// A pass over the four ordinary fixtures, whose first tick loads the progress row and stores one window.
func diagnosticPass(t *testing.T) (*CaptureStore, *repairPass) {
	t.Helper()
	s := openTestStore(t)
	for i, f := range repairFixtures() {
		e := fixtureMessage("D"+string(rune('A'+i)), 1700000000+int64(i))
		e.Message, e.RawMessage = f.msg.Message, f.msg.Message
		envelope, err := captureEnvelope(s.Config.Account, e, s.Owner, "live", s.OwnerLID)
		if err != nil {
			t.Fatal(err)
		}
		event := envelope["event"].(map[string]any)
		event["messageType"], event["body"] = "unknown", ""
		raw, _ := marshalMessage(e)
		if err = s.append(LegacyChatID(e.Info.Chat), LegacyMessageID(e), e.Info.Timestamp.Unix(), raw, envelope, false); err != nil {
			t.Fatal(err)
		}
	}
	p := newRepairPass(s)
	p.slice = time.Hour
	return s, p
}

func refusedWith(t *testing.T, p *repairPass, phase, reason string) {
	t.Helper()
	if p.lastError != "repair_storage" {
		t.Fatalf("lastError=%q, want repair_storage", p.lastError)
	}
	want := "phase=" + phase + " reason=" + reason + " "
	if got := p.storageDiagnostic(); !strings.HasPrefix(got, want) {
		t.Fatalf("diagnostic %q, want prefix %q", got, want)
	}
}

// A trigger on the pass's own table makes one statement refuse with a canary message.
func trigger(t *testing.T, s *CaptureStore, when, action string) {
	t.Helper()
	for _, q := range []string{
		`CREATE TABLE IF NOT EXISTS repair_progress(version INTEGER PRIMARY KEY,last_seq INTEGER NOT NULL,done INTEGER NOT NULL,counts TEXT NOT NULL)`,
		`CREATE TRIGGER diag_refuse ` + when + ` ON repair_progress BEGIN SELECT ` + action + `; END`,
	} {
		if _, err := s.db.Exec(q); err != nil {
			t.Fatal(err)
		}
	}
}

const canary = "CANARY-raw-error-text-/private/session.sqlite-token"

// spec: ANYM-809
func TestRepairDiagnosticIsEmptyWhenThePassStoresNormally(t *testing.T) {
	_, p := diagnosticPass(t)
	runRepairToEnd(t, p)
	if p.lastError != "" || p.storageDiagnostic() != "" {
		t.Fatalf("clean pass: err=%q diagnostic=%q", p.lastError, p.storageDiagnostic())
	}
}

// spec: ANYM-809
func TestRepairDiagnosticNamesEachHeadroomRefusal(t *testing.T) {
	t.Run("page_reserve", func(t *testing.T) {
		_, p := diagnosticPass(t)
		p.reservePages = 1 << 40 // more free pages than any file has: the load's headroom refuses
		p.tick(context.Background())
		refusedWith(t, p, "load_headroom", "page_reserve")
		if p.loaded {
			t.Fatal("a refused load must not count as loaded")
		}
	})
	t.Run("disk_reserve", func(t *testing.T) {
		s, p := diagnosticPass(t)
		var st syscall.Statfs_t
		if err := syscall.Statfs(s.Config.EventsDirectory, &st); err != nil {
			t.Fatal(err)
		}
		s.Config.DiskReserveBytes = int64(st.Bavail)*int64(st.Bsize) + 1<<40
		p.tick(context.Background())
		refusedWith(t, p, "load_headroom", "disk_reserve")
	})
	t.Run("disk_check", func(t *testing.T) {
		s, p := diagnosticPass(t)
		s.Config.EventsDirectory = filepath.Join(t.TempDir(), "absent") // statfs itself fails
		p.tick(context.Background())
		refusedWith(t, p, "load_headroom", "disk_check")
	})
	t.Run("step_headroom", func(t *testing.T) {
		_, p := diagnosticPass(t)
		reserve := p.reservePages
		calls := 0
		p.classify = func(r repairRow) string {
			if calls++; calls == 1 { // after the load's headroom passed, before the step's
				p.reservePages = 1 << 40
			}
			return p.s.repairState(r)
		}
		p.tick(context.Background())
		refusedWith(t, p, "step_headroom", "page_reserve")
		if p.lastSeq != 0 {
			t.Fatal("a refused step must not move the progress", p.lastSeq)
		}
		p.reservePages, p.lastError = reserve, ""
		runRepairToEnd(t, p) // the next ticks store normally
	})
}

// spec: ANYM-809
func TestRepairDiagnosticNamesEachRefusedWriteWithoutItsErrorText(t *testing.T) {
	for _, c := range []struct{ name, when, phase string }{
		{"delete", "BEFORE DELETE", "load_delete"},
		{"insert", "BEFORE INSERT", "load_insert"},
		{"update", "BEFORE UPDATE", "step_update"},
	} {
		t.Run(c.name, func(t *testing.T) {
			s, p := diagnosticPass(t)
			trigger(t, s, c.when, "RAISE(ABORT,'"+canary+"')")
			if c.name == "delete" { // an older version's row, which the load deletes
				if _, err := s.db.Exec("INSERT INTO repair_progress VALUES(0,0,0,'{}')"); err != nil {
					t.Fatal(err)
				}
			}
			p.tick(context.Background())
			refusedWith(t, p, c.phase, "write_error")
			if got := p.storageDiagnostic(); strings.Contains(got, "CANARY") || strings.Contains(got, "private") ||
				strings.Contains(got, "repair_progress") || strings.Contains(got, "RAISE") {
				t.Fatalf("raw error, SQL or path reached the diagnostic: %q", got)
			}
			if p.lastSeq != 0 {
				t.Fatal("a refused write must not move the progress", p.lastSeq)
			}
		})
	}
	t.Run("rows_affected", func(t *testing.T) {
		s, p := diagnosticPass(t)
		trigger(t, s, "BEFORE UPDATE", "RAISE(IGNORE)") // the UPDATE runs and changes no row
		p.tick(context.Background())
		refusedWith(t, p, "step_rows", "rows_affected")
		if p.loaded || p.lastSeq != 0 {
			t.Fatal("a missing progress row restarts the version", p.loaded, p.lastSeq)
		}
	})
}

// spec: ANYM-809
func TestRepairDiagnosticNamesAWriteThatEndedWithItsStatementContext(t *testing.T) {
	s, p := diagnosticPass(t)
	reached := 0
	// At the load's first write, and only there, the statement waits for its own context to end.
	hookWrite(t, s, "CREATE TABLE IF NOT EXISTS repair_progress", func(ctx context.Context) {
		reached++
		<-ctx.Done()
	})
	p.tick(context.Background())
	if reached != 1 {
		t.Fatal("the write was not reached", reached)
	}
	refusedWith(t, p, "load_create", "deadline")
	if got := p.storageDiagnostic(); !strings.Contains(got, " elapsed=ge_2s ") && !strings.Contains(got, " elapsed=lt_2s ") &&
		!strings.Contains(got, " elapsed=lt_500ms ") {
		t.Fatalf("no fixed elapsed bucket: %q", got)
	}
}

// spec: ANYM-809
func TestRepairDiagnosticBelongsToTheTickOfTheErrorItExplains(t *testing.T) {
	_, p := diagnosticPass(t)
	reserve := p.reservePages
	p.reservePages = 1 << 40
	p.tick(context.Background())
	first := p.storageDiagnostic()
	if !strings.HasSuffix(first, " tick=1") {
		t.Fatalf("first refusal %q, want tick=1", first)
	}
	// Later ticks store normally; the error stays sticky and so does the diagnostic of its own tick.
	p.reservePages = reserve
	p.tick(context.Background())
	if p.lastError != "repair_storage" || p.storageDiagnostic() != first {
		t.Fatalf("after success: err=%q diagnostic=%q", p.lastError, p.storageDiagnostic())
	}
	// A later read failure replaces the error: no storage diagnostic is paired with it.
	p.done = false
	p.active = func() bool { return true }
	p.s.db.Close()
	p.tick(context.Background())
	if p.lastError == "repair_storage" || p.storageDiagnostic() != "" {
		t.Fatalf("a non-storage error kept a storage diagnostic: err=%q diagnostic=%q", p.lastError, p.storageDiagnostic())
	}
}

// spec: ANYM-809
func TestRepairDiagnosticNamesAWriteEndedByTheCallersCancellation(t *testing.T) {
	s, p := diagnosticPass(t)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	reached := 0
	// The caller cancels exactly when the load's first write starts; the driver then ends that statement.
	hookWrite(t, s, "CREATE TABLE IF NOT EXISTS repair_progress", func(context.Context) {
		reached++
		cancel()
	})
	p.tick(ctx)
	if reached != 1 {
		t.Fatal("the write was not reached", reached)
	}
	refusedWith(t, p, "load_create", "canceled")
}

// hookWrite reopens the store's database through the same SQLite driver, wrapped so that a statement whose text
// starts with prefix runs hook inside its own Exec call, with its own context, before the driver runs it. Test-local:
// one connection, the store's DSN and page ceiling, nothing else changed.
func hookWrite(t *testing.T, s *CaptureStore, prefix string, hook func(context.Context)) {
	t.Helper()
	connector := hookedConnector{dsn: sqliteDSN(filepath.Join(s.Config.SessionDirectory, "capture.sqlite")), drv: s.db.Driver(),
		hook: func(ctx context.Context, query string) {
			if strings.HasPrefix(query, prefix) {
				hook(ctx)
			}
		}}
	db := sql.OpenDB(connector)
	db.SetMaxOpenConns(1)
	if _, err := db.Exec("PRAGMA max_page_count=131072"); err != nil {
		t.Fatal(err)
	}
	s.db.Close()
	s.db = db
}

type hookedConnector struct {
	dsn  string
	drv  driver.Driver
	hook func(ctx context.Context, query string)
}

func (c hookedConnector) Connect(context.Context) (driver.Conn, error) {
	conn, err := c.drv.Open(c.dsn)
	if err != nil {
		return nil, err
	}
	return hookedConn{conn, c.hook}, nil
}

func (c hookedConnector) Driver() driver.Driver { return c.drv }

type hookedConn struct {
	driver.Conn
	hook func(ctx context.Context, query string)
}

func (c hookedConn) ExecContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Result, error) {
	c.hook(ctx, query)
	return c.Conn.(driver.ExecerContext).ExecContext(ctx, query, args)
}

func (c hookedConn) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	return c.Conn.(driver.QueryerContext).QueryContext(ctx, query, args)
}

func (c hookedConn) PrepareContext(ctx context.Context, query string) (driver.Stmt, error) {
	return c.Conn.(driver.ConnPrepareContext).PrepareContext(ctx, query)
}

func (c hookedConn) BeginTx(ctx context.Context, opts driver.TxOptions) (driver.Tx, error) {
	return c.Conn.(driver.ConnBeginTx).BeginTx(ctx, opts)
}
