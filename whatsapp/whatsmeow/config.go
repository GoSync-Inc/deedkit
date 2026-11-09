package main

import (
	"encoding/json"
	"errors"
	"go.mau.fi/whatsmeow/types"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
)

type Config struct {
	Account          string `json:"account"`
	ExpectedWID      string `json:"expected_wid"`
	SessionDirectory string `json:"session_directory"`
	// ANYM-914: the canonical host lock directory; empty means /run/deedkit/session-locks.
	SessionLockDirectory string `json:"session_lock_directory"`
	EventsDirectory      string `json:"events_directory"`
	PagesDirectory       string `json:"pages_directory"`
	MaxMediaBytes        int64  `json:"max_media_bytes"`
	DiskReserveBytes     int64  `json:"disk_reserve_bytes"`
	// ANYM-814: the collector's opt-in phone repair of unverified spans (default off).
	GapRepair bool `json:"whatsapp_gap_repair"`
	// ANYM-937 serve mode: the holder's session_holder DSN file (0600); unused in child mode.
	HolderDSNFile string `json:"holder_dsn_file"`
	// Tests only: the kernel boot ID's path; empty means /proc/sys/kernel/random/boot_id.
	BootIDPath string `json:"boot_id_path"`
}
type Policy struct {
	Enabled           bool     `json:"enabled"`
	SendEnabled       bool     `json:"send_enabled"`
	OperationsEnabled []string `json:"operations_enabled"`
	DiscoverChats     bool     `json:"discover_chats"`
	IncludeChats      []string `json:"include_chats"`
	ExcludeChats      []string `json:"exclude_chats"`
	// ANYM-937: the attached collector's policy version in serve mode; 0 in child mode.
	Version int64 `json:"-"`
}

func (p Policy) Allows(chat string) bool {
	if !p.Enabled {
		return false
	}
	if _, e := ParseChatID(chat); e != nil {
		return false
	}
	for _, v := range p.ExcludeChats {
		if v == chat {
			return false
		}
	}
	if p.DiscoverChats {
		return true
	}
	for _, v := range p.IncludeChats {
		if v == chat {
			return true
		}
	}
	return false
}
func readPrivate(path string, limit int64) ([]byte, error) {
	fd, e := syscall.Open(path, syscall.O_RDONLY|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), path)
	defer f.Close()
	s, e := f.Stat()
	if e != nil {
		return nil, e
	}
	if !s.Mode().IsRegular() || s.Mode().Perm()&0077 != 0 || s.Size() > limit {
		return nil, errors.New("private_regular_file_required")
	}
	b, e := io.ReadAll(io.LimitReader(f, limit+1))
	if int64(len(b)) > limit {
		return nil, errors.New("provider_request_too_large")
	}
	return b, e
}
func loadConfig(path string) (Config, error) {
	var c Config
	b, e := readPrivate(path, 1<<20)
	if e != nil {
		return c, e
	}
	if e = json.Unmarshal(b, &c); e != nil {
		return c, errors.New("invalid_config")
	}
	if !regexp.MustCompile(`^[a-zA-Z0-9_.-]{1,64}$`).MatchString(c.Account) || !regexp.MustCompile(`^[0-9]{5,20}@c\.us$`).MatchString(c.ExpectedWID) {
		return c, errors.New("invalid_config")
	}
	if c.MaxMediaBytes == 0 {
		c.MaxMediaBytes = 512 << 20
	}
	if c.DiskReserveBytes == 0 {
		c.DiskReserveBytes = 1 << 30
	}
	if c.MaxMediaBytes < 1 || c.MaxMediaBytes > 4<<30 || c.DiskReserveBytes < 0 {
		return c, errors.New("invalid_config")
	}
	dirs := []string{c.SessionDirectory, c.EventsDirectory, c.PagesDirectory}
	for i, d := range dirs {
		if !filepath.IsAbs(d) || filepath.Clean(d) != d {
			return c, errors.New("absolute_state_path_required")
		}
		for _, other := range dirs[:i] {
			if d == other || strings.HasPrefix(d, other+string(os.PathSeparator)) || strings.HasPrefix(other, d+string(os.PathSeparator)) {
				return c, errors.New("collector_directories_overlap")
			}
		}
		if e = privateRoot(d); e != nil {
			return c, e
		}
	}
	return c, nil
}
func (p *Provider) policy() (Policy, error) {
	if p.policySource != nil {
		return p.policySource() // serve mode: only the attached collector's policy (serve.go policyNow)
	}
	var saved struct {
		ExpiresAt float64 `json:"expires_at"`
		Policy    Policy  `json:"policy"`
	}
	b, e := readPrivate(filepath.Join(p.Config.EventsDirectory, ".policy"), 1<<20)
	if e != nil {
		return Policy{}, errors.New("invalid_policy")
	}
	if json.Unmarshal(b, &saved) != nil || saved.ExpiresAt <= 0 || saved.Policy.IncludeChats == nil || saved.Policy.ExcludeChats == nil {
		return Policy{}, errors.New("invalid_policy")
	}
	if saved.ExpiresAt < float64(time.Now().UnixNano())/1e9 {
		return Policy{}, errors.New("policy_expired")
	}
	if !saved.Policy.Enabled {
		return Policy{}, errors.New("collector_disabled")
	}
	return saved.Policy, nil
}

var chatPattern = regexp.MustCompile(`^[a-zA-Z0-9_.:-]{1,140}@(c\.us|g\.us|lid)$`)

func ParseChatID(s string) (types.JID, error) {
	if !chatPattern.MatchString(s) {
		return types.EmptyJID, errors.New("send_target_invalid")
	}
	if strings.HasSuffix(s, "@c.us") {
		s = strings.TrimSuffix(s, "@c.us") + "@s.whatsapp.net"
	}
	j, e := types.ParseJID(s)
	if e != nil || j.User == "" || j.Device != 0 || j.RawAgent != 0 {
		return types.EmptyJID, errors.New("send_target_invalid")
	}
	return j, nil
}
func LegacyChatID(j types.JID) string {
	j = j.ToNonAD()
	if j.Server == types.DefaultUserServer {
		return j.User + "@c.us"
	}
	return j.String()
}
func privateRoot(root string) error {
	if !filepath.IsAbs(root) {
		return errors.New("absolute_state_path_required")
	}
	if e := os.MkdirAll(root, 0700); e != nil {
		return e
	}
	st, e := os.Lstat(root)
	if e != nil {
		return e
	}
	if !st.IsDir() || st.Mode()&os.ModeSymlink != 0 || st.Mode().Perm()&0077 != 0 {
		return errors.New("private_state_required")
	}
	return nil
}
func privateFile(root, name string) (*os.File, error) {
	if e := privateRoot(root); e != nil {
		return nil, e
	}
	fd, e := syscall.Open(filepath.Join(root, name), syscall.O_CREAT|syscall.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if e != nil {
		return nil, e
	}
	f := os.NewFile(uintptr(fd), name)
	st, e := f.Stat()
	if e != nil || !st.Mode().IsRegular() || st.Mode().Perm()&0077 != 0 {
		f.Close()
		return nil, errors.New("private_regular_file_required")
	}
	return f, nil
}
