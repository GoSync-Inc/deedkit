package main

// ANYM-937: the capture fence (CaptureStore.Fence; serve mode only, nil in child mode) and the replay publication
// it guards. Every write holds the fence across its policy check and the write itself (serve.go holder.fence).

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
)

// errFenceExcluded: the chat is excluded under the current policy; the event is discarded, not written.
var errFenceExcluded = errors.New("chat_excluded")

func (s *CaptureStore) fence(chat string) (func(), error) {
	if s.Fence == nil {
		return func() {}, nil
	}
	return s.Fence(chat)
}

type pendingCapture struct {
	d, chat string
	b       []byte
}

// publishPending exports journaled rows; callers hold mu. A row whose chat the current policy excludes is never
// exported and counts as consumed; an expired policy stops the batch.
func (s *CaptureStore) publishPending(pending []pendingCapture, usage *spoolUsage) error {
	for _, i := range pending {
		release, e := s.fence(i.chat)
		if e == errFenceExcluded {
			if _, e = s.db.Exec("UPDATE capture SET published=1 WHERE digest=?", i.d); e != nil {
				return e
			}
			continue
		}
		if e != nil {
			return e
		}
		e = s.publishWithUsage(i.d, i.b, usage)
		release()
		if e != nil {
			return e
		}
	}
	return nil
}

// fenceRefusal: an excluded chat's event is discarded (nil, so it is acknowledged); any other refusal is returned.
func fenceRefusal(err error) error {
	if err == errFenceExcluded {
		return nil
	}
	return err
}

// captureDigest is append's content key (moved here unchanged to keep store.go inside its size budget).
func captureDigest(envelope map[string]any, raw []byte) string {
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
	return hex.EncodeToString(h.Sum(nil))
}
