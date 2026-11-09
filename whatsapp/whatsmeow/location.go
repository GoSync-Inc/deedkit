package main

import (
	"math"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
)

// sharedLocation is a location message's kind, body and block (ANYM-922). A point or venue keeps its name as the
// body; a live share keeps its caption and is archived once, as the fact {live: true}, never a point of its track.
// A later position carries the same ID and fields, so it makes no new row or revision.
func sharedLocation(m *waE2E.Message) (string, string, map[string]any) {
	if v := m.GetLiveLocationMessage(); v != nil {
		return "location", v.GetCaption(), map[string]any{"live": true}
	}
	return "location", m.GetLocationMessage().GetName(), locationBlock(m.GetLocationMessage())
}

// withBlocks adds each captured block under its key. An absent one is no key, so an older reader never meets the
// field (rollback).
func withBlocks(value map[string]any, blocks map[string]map[string]any) {
	for key, block := range blocks {
		if block != nil {
			value[key] = block
		}
	}
}

// locationBlock is a shared point or venue as the sender stated it: coordinates, accuracy, name and address
// (contactText bounds: no control characters, 256 code points). One flagged live is only {live: true}. The archive
// validates the range and drops a block it cannot accept, never the message.
func locationBlock(v *waE2E.LocationMessage) map[string]any {
	if v.GetIsLive() {
		return map[string]any{"live": true}
	}
	text := func(s string) any {
		if s, _ = contactText(s); s != "" {
			return s
		}
		return nil
	}
	// JSON has no NaN or infinity: such a coordinate is sent as null, so the archive drops the block, not the message.
	coordinate := func(f float64) any {
		if math.IsNaN(f) || math.IsInf(f, 0) {
			return nil
		}
		return f
	}
	var accuracy any
	if v.AccuracyInMeters != nil {
		accuracy = v.GetAccuracyInMeters()
	}
	return map[string]any{"latitude": coordinate(v.GetDegreesLatitude()), "longitude": coordinate(v.GetDegreesLongitude()),
		"accuracy_m": accuracy, "title": text(v.GetName()), "address": text(v.GetAddress()), "live": false}
}
