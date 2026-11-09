package main

import (
	"context"
	"encoding/json"
	"errors"
	"flag"
	"github.com/skip2/go-qrcode"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types/events"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"syscall"
	"time"
)

func acquire(root string) (*os.File, error) {
	f, e := privateFile(root, "native-owner.lock")
	if e != nil {
		return nil, e
	}
	if e = syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); e != nil {
		f.Close()
		return nil, errors.New("session_already_owned")
	}
	return f, nil
}

// ANYM-914: the Python parent holds the canonical host lock, the per-directory lock and the claim row, and is their
// only releaser. Before any store open this child requires the inherited canonical lock descriptor: the same file
// as <session_lock_directory>/<account>.lock and a flock that succeeds on it (the same open file description).
const defaultSessionLockDirectory = "/run/deedkit/session-locks"

func inheritedClaim(c Config) error {
	refused := errors.New("session_claim_required")
	value := os.Getenv("ANYMSG_SESSION_CLAIM_FD")
	fd, e := strconv.Atoi(value)
	if value == "" || e != nil || fd < 3 {
		return refused
	}
	directory := c.SessionLockDirectory
	if directory == "" {
		directory = defaultSessionLockDirectory
	}
	var inherited, named syscall.Stat_t
	if syscall.Fstat(fd, &inherited) != nil || syscall.Lstat(filepath.Join(directory, c.Account+".lock"), &named) != nil {
		return refused
	}
	if inherited.Dev != named.Dev || inherited.Ino != named.Ino || inherited.Mode&syscall.S_IFMT != syscall.S_IFREG {
		return refused
	}
	if syscall.Flock(fd, syscall.LOCK_EX|syscall.LOCK_NB) != nil {
		return refused
	}
	return nil
}
func main() {
	syscall.Umask(0077)
	var path string
	var pair bool
	var fixture string
	flag.StringVar(&path, "config", "", "private collector config")
	flag.BoolVar(&pair, "pair", false, "explicit device linking; private QR PNG only")
	flag.StringVar(&fixture, "normalize-fixture", "", "offline synthetic fixture; no provider/session access")
	var menuFixture string
	flag.StringVar(&menuFixture, "menu-answer-fixture", "", "offline synthetic menu choice through sendText with fake dependencies; no provider/session access")
	var mediaKeys bool
	flag.BoolVar(&mediaKeys, "media-keys", false, "read-only capture journal lookup for the operator media inventory; no provider/session access")
	var menuShapes bool
	flag.BoolVar(&menuShapes, "menu-reply-shapes", false, "read-only capture journal scan: field relations of the owner's menu replies, no values (ANYM-837); no provider/session access")
	var socketDir string
	flag.StringVar(&socketDir, "serve", "", "session holder (ANYM-937): own the session and serve the collector on unix sockets in this 0700 directory")
	var version bool
	flag.BoolVar(&version, "version", false, "print the build's source SHA and holder protocol range; no provider/session access")
	flag.Parse()
	if version {
		_ = json.NewEncoder(os.Stdout).Encode(map[string]any{"holder_source_sha": sourceSHA, "protocol": holderProtocol, "supported": holderSupported})
		return
	}
	code := 0
	var err error
	if fixture != "" {
		err = normalizeFixture(fixture)
	} else if menuFixture != "" {
		err = menuAnswerFixture(menuFixture, os.Stdout)
	} else if mediaKeys {
		err = serveMediaKeys(path, os.Stdin, os.Stdout)
	} else if menuShapes {
		err = serveMenuReplyShapes(path, os.Stdout)
	} else if socketDir != "" {
		err = serve(path, socketDir)
	} else {
		err = execute(path, pair)
	}
	if e := err; e != nil {
		code = 1
		if pair && e.Error() == "whatsapp_identity_mismatch" {
			code = 2
		}
		_ = json.NewEncoder(os.Stderr).Encode(map[string]any{"state": "failed", "reason": safeError(e)})
	}
	os.Exit(code)
}
func execute(path string, pair bool) error {
	c, e := loadConfig(path)
	if e != nil {
		return e
	}
	if e = inheritedClaim(c); e != nil {
		return e
	}
	lock, e := acquire(c.SessionDirectory)
	if e != nil {
		return e
	}
	defer lock.Close()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, syscall.SIGINT)
	defer stop()
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	if pair {
		return pairSession(ctx, c)
	}
	sending, e := sendingFile()
	if e != nil {
		return e
	}
	defer sending.Close()
	input, e := inheritedIPCFile(0, "background-input")
	if e != nil {
		return e
	}
	defer input.Close()
	output, e := inheritedIPCFile(1, "background-output")
	if e != nil {
		return e
	}
	defer output.Close()
	p := &Provider{Config: c}
	defer p.Close()
	lanes := make(chan error, 2)
	go func() {
		lanes <- runLane(ctx, p, input, output, map[string]bool{"start": true, "policy": true, "dialogs": true, "page": true, "media": true, "request_history": true, "gap_repair": true, "gap_repair_status": true})
	}()
	go func() {
		lanes <- runLane(ctx, p, sending, sending, map[string]bool{"send_text": true, "messenger_operation": true})
	}()
	received := 0
	select {
	case e = <-lanes:
		received = 1
		if ctx.Err() != nil {
			// A lane may observe the signal before this select does. Normal
			// cancellation is still a graceful process exit in that ordering.
			e = nil
		}
		cancel()
	case <-ctx.Done():
	}
	_ = input.Close()
	_ = sending.Close()
	_ = output.Close()
	// Readers and in-flight handlers own the stores until their lane has
	// drained/cancelled. Never close SQLite beneath a live capture/send call.
	for received < 2 {
		<-lanes
		received++
	}
	return e
}
func pairSession(ctx context.Context, c Config) error {
	if info, err := os.Stderr.Stat(); err != nil || info.Mode()&os.ModeCharDevice == 0 {
		return errors.New("owner_terminal_required")
	}

	ctx, cancel := context.WithTimeout(ctx, 4*time.Minute)
	defer cancel()
	defer os.Remove(filepath.Join(c.SessionDirectory, "qr.png"))
	status := func(state string) error {
		v := map[string]any{"state": state, "observed_at": time.Now().UTC().Format(time.RFC3339Nano)}
		if e := atomicPrivate(c.SessionDirectory, ".pair-status.json", mustJSON(v)); e != nil {
			return e
		}
		return json.NewEncoder(os.Stdout).Encode(v)
	}
	f, e := privateFile(c.SessionDirectory, "auth.sqlite")
	if e != nil {
		return e
	}
	f.Close()
	db, e := sqlstore.New(ctx, "sqlite3", sqliteDSN(filepath.Join(c.SessionDirectory, "auth.sqlite")), nil)
	if e != nil {
		return e
	}
	defer db.Close()
	dev, e := db.GetFirstDevice(ctx)
	if e != nil {
		return e
	}
	if dev.ID != nil && LegacyChatID(dev.ID.ToNonAD()) != c.ExpectedWID {
		return errors.New("whatsapp_identity_mismatch")
	}
	cli := nativeClient(dev)
	defer cli.Disconnect()
	connected := make(chan struct{}, 2)
	fail := make(chan error, 1)
	cli.AddEventHandlerWithSuccessStatus(func(value any) bool {
		switch value.(type) {
		case *events.Connected:
			if cli.Store.ID == nil || LegacyChatID(cli.Store.ID.ToNonAD()) != c.ExpectedWID {
				select {
				case fail <- errors.New("whatsapp_identity_mismatch"):
				default:
				}
				return false
			}
			select {
			case connected <- struct{}{}:
			default:
			}
		case *events.Message:
			return false // initial content is retried after the normal durable collector starts
		}
		return true
	})
	var qr <-chan whatsmeow.QRChannelItem
	if dev.ID == nil {
		qr, e = cli.GetQRChannel(ctx)
		if e != nil {
			return e
		}
	}
	if e = cli.ConnectContext(ctx); e != nil {
		return errors.New("provider_initialization_failed")
	}
	for {
		select {
		case item, ok := <-qr:
			if !ok {
				qr = nil
				continue
			}
			if item.Event == "code" {
				png, e := qrcode.Encode(item.Code, qrcode.Low, 512)
				if e != nil {
					return e
				}
				if e = atomicPrivate(c.SessionDirectory, "qr.png", png); e != nil {
					return e
				}
				code, err := qrcode.New(item.Code, qrcode.Low)
				if err != nil {
					return err
				}
				if _, err = os.Stderr.WriteString(code.ToSmallString(false) + "\n"); err != nil {
					return err
				}
				if e = status("qr_ready"); e != nil {
					return e
				}
			} else if item.Event == "timeout" {
				return errors.New("whatsapp_pairing_required")
			}
		case <-connected:
			_ = os.Remove(filepath.Join(c.SessionDirectory, "qr.png"))
			if e = status("linked_verifying_restore"); e != nil {
				return e
			}
			cli.Disconnect()
			// Prove durable credentials survive a fresh Client before claiming success.
			saved, e := db.GetFirstDevice(ctx)
			if e != nil || saved.ID == nil {
				return errors.New("provider_auth_failed")
			}
			restored := nativeClient(saved)
			defer restored.Disconnect()
			ready := make(chan bool, 1)
			restored.AddEventHandlerWithSuccessStatus(func(v any) bool {
				if _, ok := v.(*events.Connected); ok {
					select {
					case ready <- restored.Store.ID != nil && LegacyChatID(restored.Store.ID.ToNonAD()) == c.ExpectedWID:
					default:
					}
				}
				if _, ok := v.(*events.Message); ok {
					return false
				}
				return true
			})
			if e = restored.ConnectContext(ctx); e != nil {
				return errors.New("provider_auth_failed")
			}
			select {
			case verified := <-ready:
				if !verified {
					return errors.New("whatsapp_identity_mismatch")
				}
				return status("authenticated")
			case <-ctx.Done():
				return errors.New("provider_auth_failed")
			case <-time.After(45 * time.Second):
				return errors.New("provider_auth_failed")
			}
		case e := <-fail:
			return e
		case <-ctx.Done():
			return errors.New("whatsapp_pairing_required")
		}
	}
}
