package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"testing"
	"time"
)

func TestOversizedDialogsAnswerWithErrorFrameAndKeepLane(t *testing.T) {
	p := skipProvider(t)
	// A synthetic catalog whose JSON result exceeds the 1 MiB frame limit.
	title := strings.Repeat("t", 2000)
	for i := 0; i < 600; i++ {
		if _, err := p.Capture.db.Exec("INSERT INTO chats(chat,title,observed_at)VALUES(?,?,?)", fmt.Sprintf("%d@c.us", 100000+i), title, time.Now().UTC().Format(time.RFC3339Nano)); err != nil {
			t.Fatal(err)
		}
	}
	in := strings.NewReader("{\"id\":1,\"method\":\"dialogs\",\"params\":{\"limit\":10000}}\n{\"id\":2,\"method\":\"dialogs\",\"params\":{\"limit\":5}}\n")
	var out bytes.Buffer
	if err := runLane(context.Background(), p, in, &out, map[string]bool{"dialogs": true}); err != nil {
		t.Fatal("oversized result ended the lane", err)
	}
	scanner := bufio.NewScanner(&out)
	scanner.Buffer(make([]byte, 4096), 1<<20)
	var frames []map[string]any
	for scanner.Scan() {
		var frame map[string]any
		if err := json.Unmarshal(scanner.Bytes(), &frame); err != nil {
			t.Fatal(err)
		}
		frames = append(frames, frame)
	}
	if len(frames) != 2 || frames[0]["id"] != float64(1) || frames[0]["error"] != "provider_response_too_large" || frames[0]["result"] != nil {
		t.Fatal("oversized dialogs not answered by one bounded error frame", len(frames))
	}
	if dialogs, _ := frames[1]["result"].([]any); frames[1]["id"] != float64(2) || len(dialogs) != 5 {
		t.Fatal("lane not synchronized after the error frame", frames[1])
	}
}

// spec: ANYM-388
func TestLaneRequestIsOneJSONLineOfAtMostOneMiB(t *testing.T) {
	// A request padded with JSON whitespace to a total line length (newline
	// included) of n bytes.
	line := func(id, n int) string {
		head := fmt.Sprintf("{\"id\":%d,\"method\":\"dialogs\",\"params\":{\"limit\":5}", id)
		return head + strings.Repeat(" ", n-len(head)-2) + "}\n"
	}
	frames := func(out *bytes.Buffer) []map[string]any {
		var result []map[string]any
		for _, raw := range strings.Split(strings.TrimSpace(out.String()), "\n") {
			var frame map[string]any
			if raw != "" && json.Unmarshal([]byte(raw), &frame) == nil {
				result = append(result, frame)
			}
		}
		return result
	}
	p := skipProvider(t)
	var out bytes.Buffer
	if err := runLane(context.Background(), p, strings.NewReader(line(1, 1<<20)), &out, map[string]bool{"dialogs": true}); err != nil {
		t.Fatal("1 MiB request line ended the lane", err)
	}
	if got := frames(&out); len(got) != 1 || got[0]["id"] != float64(1) || got[0]["error"] != nil || got[0]["result"] == nil {
		t.Fatal("1 MiB request line not answered", got)
	}
	out.Reset()
	err := runLane(context.Background(), p, strings.NewReader(line(1, 1<<20+1)+line(2, 64)), &out, map[string]bool{"dialogs": true})
	if !errors.Is(err, bufio.ErrTooLong) || out.Len() != 0 {
		t.Fatal("request line over 1 MiB was read or answered", err, out.Len())
	}
}
