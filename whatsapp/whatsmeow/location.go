package main

import (
	"math"

	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// withoutLiveTrack returns the native evidence the journal keeps (a wire message or a history WebMessageInfo) with
// every live-location position removed, at any depth (wrappers, quotes): a live share leaves no track on disk, and a
// position-only update is byte-identical, so the journal keeps one row for it (ANYM-922, Astra r1 B1). Evidence
// without a live location is returned as is.
func withoutLiveTrack[T proto.Message](m T) T {
	if !liveTrack(m.ProtoReflect(), false) {
		return m
	}
	clean := proto.Clone(m).(T)
	liveTrack(clean.ProtoReflect(), true)
	return clean
}

// liveTrack reports whether m holds a live location; with scrub it keeps only a live message's caption, name, live
// flag and context (its quote, itself scrubbed) and drops every other field: coordinates, accuracy, speed, heading,
// sequence, time offset, map thumbnail and unknown fields.
func liveTrack(m protoreflect.Message, scrub bool) bool {
	if !m.IsValid() {
		return false
	}
	found := false
	var fields []protoreflect.FieldDescriptor
	m.Range(func(fd protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		fields = append(fields, fd)
		switch {
		case fd.Message() == nil:
		case fd.IsList():
			for i := 0; i < v.List().Len(); i++ {
				found = liveTrack(v.List().Get(i).Message(), scrub) || found
			}
		case fd.IsMap():
			if fd.MapValue().Message() != nil {
				v.Map().Range(func(_ protoreflect.MapKey, x protoreflect.Value) bool {
					found = liveTrack(x.Message(), scrub) || found
					return true
				})
			}
		default:
			found = liveTrack(v.Message(), scrub) || found
		}
		return true
	})
	live := false
	switch x := m.Interface().(type) {
	case *waE2E.LiveLocationMessage:
		live = true
	case *waE2E.LocationMessage:
		live = x.GetIsLive()
	}
	if live && scrub {
		for _, fd := range fields {
			if name := fd.Name(); name != "caption" && name != "name" && name != "isLive" && name != "contextInfo" {
				m.Clear(fd)
			}
		}
		m.SetUnknown(nil)
	}
	return found || live
}

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
