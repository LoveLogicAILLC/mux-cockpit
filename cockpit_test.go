package main

import (
	"bufio"
	"net"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func fakeHost(t *testing.T, reply string) string {
	t.Helper()
	sock := filepath.Join(t.TempDir(), "h.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { ln.Close() })
	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func(c net.Conn) {
				defer c.Close()
				if _, err := bufio.NewReader(c).ReadBytes('\n'); err == nil {
					c.Write([]byte(reply + "\n"))
				}
			}(c)
		}
	}()
	return sock
}

const sample = `{"host":"HOST: m4","provider":"ollama","uptime":5,"mux_depth":2,
"workers":[{"id":"planner","role":"PLANNER","status":"running","tokens":4200,"context":900,"load":38,"lora":"lora_planner_r16","spilled":1,"completed":2,"score":7.5,"current":"ab12","todos":[{"content":"a\nb","status":"in_progress"}]}],
"channels":[{"channel":"INPUT","queue":2,"worker":"planner","state":"active"}],
"quota":[{"resource":"Checkpoints","used":3,"limit":0,"state":"NVMe"}],
"events":["12:00 x"],"git_log":["abc: y"],"roles":["PLANNER","CRITIC"]}`

func TestStatusPollAndRender(t *testing.T) {
	sock := fakeHost(t, strings.ReplaceAll(sample, "\n", ""))
	m := initialModel(sock)
	msg := m.pollCmd()().(statusMsg)
	if msg.err != nil {
		t.Fatal(msg.err)
	}
	nm, _ := m.Update(msg)
	m = nm.(model)
	m.width, m.height = 150, 44
	if !m.online {
		t.Fatal("expected online")
	}
	row := m.agents.Rows()[0]
	if row[2] != "running+1" || row[3] != "4.2k" || row[7] != "7.5" {
		t.Fatalf("bad agent row %v", row)
	}
	if m.quota.Rows()[0][2] != "∞" {
		t.Fatalf("limit 0 should render ∞: %v", m.quota.Rows()[0])
	}
	v := m.View()
	for _, want := range []string{"planner • PLANNER", "[~] a b", "abc: y", "12:00 x"} {
		if !strings.Contains(v, want) {
			t.Errorf("view missing %q", want)
		}
	}
}

func TestOfflineAndActionError(t *testing.T) {
	m := initialModel(filepath.Join(t.TempDir(), "nope.sock"))
	nm, _ := m.Update(m.pollCmd()())
	if nm.(model).online || !strings.Contains(nm.(model).View(), "HOST OFFLINE") {
		t.Fatal("expected offline view")
	}
	sock := fakeHost(t, `{"status":"error","error":"unknown worker zz"}`)
	m = initialModel(sock)
	am := m.actionCmd(map[string]any{"action": "park", "worker_id": "zz"})().(actionMsg)
	if am.err == nil || !strings.Contains(am.err.Error(), "unknown worker") {
		t.Fatalf("expected surfaced error, got %+v", am)
	}
}

func TestRequestTimeout(t *testing.T) {
	sock := filepath.Join(t.TempDir(), "slow.sock")
	ln, _ := net.Listen("unix", sock)
	defer ln.Close()
	go func() { c, _ := ln.Accept(); time.Sleep(2 * time.Second); c.Close() }()
	start := time.Now()
	_, _, err := request(sock, map[string]any{"action": "status"}, 300*time.Millisecond)
	if err == nil || time.Since(start) > time.Second {
		t.Fatalf("expected fast timeout, err=%v", err)
	}
}
