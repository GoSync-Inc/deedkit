package main

// ANYM-937 criterion 5: the holder's messenger-operation outcome journal. The key is the globally unique command row
// ID (messenger_commands.id, migration 025) and the step, never the principal-scoped request ID. The pending record is
// durable before the provider call and the verified outcome before the reply; a request whose key is pending without
// a call in flight (the holder ended mid-call) never sends and answers operation_outcome_unknown. The collector
// reconciles from these files after its own restart, as it does from the text-send receipts (send.go).

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"time"
)

const operationJournalDirectory = "operation-outcomes"
const operationJournalEntries = 1000
const operationJournalKeep = 24 * time.Hour

var commandRowPattern = regexp.MustCompile(`^op_[a-f0-9]{32}$`)

type operationOutcome struct {
	Kind          string          `json:"kind"`
	Account       string          `json:"account"`
	CommandRowID  string          `json:"command_row_id"`
	Step          int             `json:"step"`
	PayloadDigest string          `json:"payload_digest"`
	Pending       bool            `json:"pending"`
	Result        json.RawMessage `json:"result,omitempty"`
	Error         string          `json:"error,omitempty"`
	ObservedAt    string          `json:"observed_at"`
}

// splitOperationKey takes the journal key out of the parameters; the rest is what Operation decodes strictly.
func splitOperationKey(raw json.RawMessage) (id string, step int, operation string, rest json.RawMessage, err error) {
	var fields map[string]json.RawMessage
	if json.Unmarshal(raw, &fields) != nil {
		return "", 0, "", nil, errors.New("operation_invalid_parameters")
	}
	if json.Unmarshal(fields["command_row_id"], &id) != nil || !commandRowPattern.MatchString(id) {
		return "", 0, "", nil, errors.New("operation_invalid_parameters")
	}
	if value, ok := fields["step"]; ok && (json.Unmarshal(value, &step) != nil || step < 0 || step > 1000) {
		return "", 0, "", nil, errors.New("operation_invalid_parameters")
	}
	_ = json.Unmarshal(fields["operation"], &operation)
	delete(fields, "command_row_id")
	delete(fields, "step")
	rest, err = json.Marshal(fields)
	return id, step, operation, rest, err
}

func (h *holder) journalPath(id string, step int) string {
	return filepath.Join(h.cfg.EventsDirectory, operationJournalDirectory, fmt.Sprintf("%s.%d.json", id, step))
}

func (h *holder) readOutcome(id string, step int) (*operationOutcome, error) {
	raw, e := readPrivate(h.journalPath(id, step), 2<<20)
	if os.IsNotExist(e) {
		return nil, nil
	}
	if e != nil {
		return nil, e
	}
	var v operationOutcome
	if json.Unmarshal(raw, &v) != nil || v.CommandRowID != id || v.Step != step {
		return nil, errors.New("operation_outcome_unknown")
	}
	return &v, nil
}

func (h *holder) writeOutcome(v operationOutcome) error {
	directory := filepath.Join(h.cfg.EventsDirectory, operationJournalDirectory)
	if e := privateRoot(directory); e != nil {
		return e
	}
	if v.Pending {
		if e := pruneOutcomes(directory); e != nil {
			return e
		}
	}
	v.Kind, v.Account, v.ObservedAt = "messenger_operation_outcome", h.cfg.Account, time.Now().UTC().Format(time.RFC3339Nano)
	return atomicPrivate(directory, filepath.Base(h.journalPath(v.CommandRowID, v.Step)), mustJSON(v))
}

// pruneOutcomes keeps the journal bounded: settled records older than a day go first; a full journal refuses a new
// operation before any provider call.
func pruneOutcomes(directory string) error {
	entries, e := os.ReadDir(directory)
	if e != nil {
		return e
	}
	if len(entries) < operationJournalEntries {
		return nil
	}
	type aged struct {
		name string
		at   time.Time
	}
	var old []aged
	for _, entry := range entries {
		info, e := entry.Info()
		if e != nil || !info.Mode().IsRegular() || time.Since(info.ModTime()) < operationJournalKeep {
			continue
		}
		raw, e := readPrivate(filepath.Join(directory, entry.Name()), 2<<20)
		var v operationOutcome
		if e != nil || json.Unmarshal(raw, &v) != nil || v.Pending {
			continue // a pending record stays: it is the only proof a call may have happened
		}
		old = append(old, aged{entry.Name(), info.ModTime()})
	}
	sort.Slice(old, func(i, j int) bool { return old[i].at.Before(old[j].at) })
	for i := 0; i < len(old) && len(entries)-i >= operationJournalEntries; i++ {
		_ = os.Remove(filepath.Join(directory, old[i].name))
	}
	if left, e := os.ReadDir(directory); e != nil || len(left) >= operationJournalEntries {
		return errors.New("spool_full")
	}
	return nil
}

func journalReply(v *operationOutcome) (any, error) {
	if v.Pending {
		return nil, errors.New("operation_outcome_unknown")
	}
	if v.Error != "" {
		return nil, errors.New(v.Error)
	}
	var result any
	if json.Unmarshal(v.Result, &result) != nil {
		return nil, errors.New("operation_outcome_unknown")
	}
	return result, nil
}

// journaledOperation runs one messenger operation at most once per key. Reads (operations.go readOperations) change
// nothing and are not journaled.
func (h *holder) journaledOperation(p providerConn, r request) (any, error) {
	id, step, operation, rest, e := splitOperationKey(r.Params)
	if e != nil {
		return nil, e
	}
	call := request{ID: r.ID, Method: r.Method, Params: rest}
	if readOperations[operation] {
		return p.handle(h.ctx, call, nil)
	}
	sum := sha256.Sum256(rest)
	digest := hex.EncodeToString(sum[:])
	key := fmt.Sprintf("%s.%d", id, step)
	for {
		h.jmu.Lock()
		running, busy := h.inflight[key]
		if !busy {
			break
		}
		h.jmu.Unlock()
		select {
		case <-running: // the first request's verified outcome is durable before it returns
		case <-h.ctx.Done():
			return nil, errors.New("operation_outcome_unknown")
		}
	}
	existing, e := h.readOutcome(id, step)
	if e != nil || existing != nil {
		h.jmu.Unlock()
		if e != nil {
			return nil, errors.New("operation_outcome_unknown")
		}
		if existing.PayloadDigest != digest {
			return nil, errors.New("operation_invalid_parameters")
		}
		return journalReply(existing)
	}
	running := make(chan struct{})
	h.inflight[key] = running
	h.jmu.Unlock()
	defer func() {
		h.jmu.Lock()
		delete(h.inflight, key)
		close(running)
		h.jmu.Unlock()
	}()
	if e = h.writeOutcome(operationOutcome{CommandRowID: id, Step: step, PayloadDigest: digest, Pending: true}); e != nil {
		return nil, e // nothing was sent
	}
	result, callErr := p.handle(h.ctx, call, nil)
	settled := operationOutcome{CommandRowID: id, Step: step, PayloadDigest: digest}
	if callErr != nil {
		settled.Error = safeError(callErr)
	} else if settled.Result, e = json.Marshal(result); e != nil {
		return nil, errors.New("operation_outcome_unknown")
	}
	if e = h.writeOutcome(settled); e != nil {
		// The call happened but its outcome is not durable: the conservative answer, never a retry.
		return nil, errors.New("operation_outcome_unknown")
	}
	return result, callErr
}
