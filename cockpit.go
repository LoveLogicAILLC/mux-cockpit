package main

// cockpit.go — Charm TUI cockpit for the MUX host-driven agent swarm.
// Talks JSON lines to host_orchestrator.py over a Unix socket (default /tmp/mux_host.sock).
//
// Keys: tab focus • ↑/↓ select agent • m morph • p park/unpark • s checkpoint
//       n new goal (AgentsRoom NEEDS INPUT) • i prompt selected worker (Pi RPC) • a abort
//       q quit

import (
	"bufio"
	"encoding/json"
	"flag"
	"fmt"
	"net"
	"os"
	"strings"
	"time"

	"github.com/charmbracelet/bubbles/table"
	"github.com/charmbracelet/bubbles/textinput"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

var version = "dev"

// ------------------------------------------------------------------ protocol

type Todo struct {
	Content string `json:"content"`
	Status  string `json:"status"`
}

type Worker struct {
	ID        string   `json:"id"`
	Role      string   `json:"role"`
	Status    string   `json:"status"`
	Tokens    int      `json:"tokens"`
	Context   int      `json:"context"`
	Load      int      `json:"load"`
	LoRA      string   `json:"lora"`
	Spilled   int      `json:"spilled"`
	Completed int      `json:"completed"`
	Score     *float64 `json:"score"`
	Current   *string  `json:"current"`
	Todos     []Todo   `json:"todos"`
}

type ChannelStat struct {
	Channel string `json:"channel"`
	Queue   int    `json:"queue"`
	Worker  string `json:"worker"`
	State   string `json:"state"`
}

type QuotaRow struct {
	Resource string `json:"resource"`
	Used     int    `json:"used"`
	Limit    int    `json:"limit"`
	State    string `json:"state"`
}

type Status struct {
	Host     string        `json:"host"`
	Provider string        `json:"provider"`
	Uptime   int           `json:"uptime"`
	MuxDepth int           `json:"mux_depth"`
	Workers  []Worker      `json:"workers"`
	Channels []ChannelStat `json:"channels"`
	Quota    []QuotaRow    `json:"quota"`
	Events   []string      `json:"events"`
	GitLog   []string      `json:"git_log"`
	Roles    []string      `json:"roles"`
}

func request(sock string, cmd map[string]any, timeout time.Duration) (map[string]any, []byte, error) {
	conn, err := net.DialTimeout("unix", sock, timeout)
	if err != nil {
		return nil, nil, err
	}
	defer conn.Close()
	_ = conn.SetDeadline(time.Now().Add(timeout))
	b, _ := json.Marshal(cmd)
	if _, err := conn.Write(append(b, '\n')); err != nil {
		return nil, nil, err
	}
	r := bufio.NewReaderSize(conn, 1<<20)
	line, err := r.ReadBytes('\n')
	if err != nil {
		return nil, nil, err
	}
	var out map[string]any
	if err := json.Unmarshal(line, &out); err != nil {
		return nil, line, err
	}
	return out, line, nil
}

// ------------------------------------------------------------------ messages

type tickMsg time.Time
type statusMsg struct {
	st  *Status
	err error
}
type actionMsg struct {
	text string
	err  error
}

func tick() tea.Cmd {
	return tea.Tick(500*time.Millisecond, func(t time.Time) tea.Msg { return tickMsg(t) })
}

func (m model) pollCmd() tea.Cmd {
	sock := m.sock
	return func() tea.Msg {
		_, raw, err := request(sock, map[string]any{"action": "status"}, time.Second)
		if err != nil {
			return statusMsg{err: err}
		}
		var st Status
		if err := json.Unmarshal(raw, &st); err != nil {
			return statusMsg{err: err}
		}
		return statusMsg{st: &st}
	}
}

func (m model) actionCmd(cmd map[string]any) tea.Cmd {
	sock := m.sock
	return func() tea.Msg {
		out, _, err := request(sock, cmd, 15*time.Second)
		if err != nil {
			return actionMsg{err: err}
		}
		if e, ok := out["error"]; ok && e != nil {
			return actionMsg{err: fmt.Errorf("%v", e)}
		}
		if s, ok := out["success"].(bool); ok && !s {
			return actionMsg{err: fmt.Errorf("%v", out["error"])}
		}
		b, _ := json.Marshal(out)
		t := string(b)
		if len(t) > 160 {
			t = t[:160] + "…"
		}
		return actionMsg{text: t}
	}
}

// ------------------------------------------------------------------ model

type mode int

const (
	modeNormal mode = iota
	modeMorph
	modeSubmit
	modePrompt
)

type model struct {
	sock    string
	st      *Status
	online  bool
	lastErr string
	flash   string
	polling bool
	focus   int // 0 agents, 1 mux, 2 quota
	mode    mode
	roleIdx int
	agents  table.Model
	mux     table.Model
	quota   table.Model
	input   textinput.Model
	width   int
	height  int
}

var (
	accent   = lipgloss.Color("86")
	dim      = lipgloss.Color("240")
	good     = lipgloss.Color("10")
	warn     = lipgloss.Color("214")
	bad      = lipgloss.Color("196")
	boxStyle = lipgloss.NewStyle().Border(lipgloss.RoundedBorder()).BorderForeground(dim).Padding(0, 1)
	focusBox = boxStyle.BorderForeground(accent)
	hdr      = lipgloss.NewStyle().Bold(true).Foreground(accent)
	muted    = lipgloss.NewStyle().Foreground(dim)
)

func newTable(cols []table.Column, h int, focused bool) table.Model {
	t := table.New(table.WithColumns(cols), table.WithHeight(h), table.WithFocused(focused))
	s := table.DefaultStyles()
	s.Header = s.Header.BorderStyle(lipgloss.NormalBorder()).BorderForeground(dim).BorderBottom(true).Bold(true)
	s.Selected = s.Selected.Foreground(lipgloss.Color("229")).Background(lipgloss.Color("57")).Bold(false)
	t.SetStyles(s)
	return t
}

func initialModel(sock string) model {
	ti := textinput.New()
	ti.CharLimit = 2000
	ti.Width = 70
	return model{
		sock: sock,
		agents: newTable([]table.Column{
			{Title: "AGENT", Width: 10}, {Title: "ROLE", Width: 17}, {Title: "STATUS", Width: 12},
			{Title: "TOKENS", Width: 7}, {Title: "CTX", Width: 6}, {Title: "LOAD", Width: 5},
			{Title: "DONE", Width: 4}, {Title: "SCORE", Width: 5}, {Title: "LORA", Width: 16},
		}, 8, true),
		mux: newTable([]table.Column{
			{Title: "CHANNEL", Width: 8}, {Title: "QUEUE", Width: 5}, {Title: "WORKER", Width: 8}, {Title: "STATE", Width: 12},
		}, 4, false),
		quota: newTable([]table.Column{
			{Title: "RESOURCE", Width: 15}, {Title: "USED", Width: 8}, {Title: "LIMIT", Width: 8}, {Title: "STATE", Width: 10},
		}, 5, false),
		input: ti,
	}
}

func (m model) Init() tea.Cmd { return tea.Batch(m.pollCmd(), tick()) }

func kfmt(n int) string {
	if n >= 1000 {
		return fmt.Sprintf("%.1fk", float64(n)/1000)
	}
	return fmt.Sprintf("%d", n)
}

func (m *model) applyStatus(st *Status) {
	m.st = st
	rows := make([]table.Row, 0, len(st.Workers))
	for _, w := range st.Workers {
		sc := "-"
		if w.Score != nil {
			sc = fmt.Sprintf("%.1f", *w.Score)
		}
		status := w.Status
		if w.Spilled > 0 {
			status = fmt.Sprintf("%s+%d", status, w.Spilled)
		}
		rows = append(rows, table.Row{w.ID, w.Role, status, kfmt(w.Tokens), kfmt(w.Context),
			fmt.Sprintf("%d%%", w.Load), fmt.Sprintf("%d", w.Completed), sc, w.LoRA})
	}
	m.agents.SetRows(rows)
	mr := []table.Row{}
	for _, c := range st.Channels {
		mr = append(mr, table.Row{c.Channel, fmt.Sprintf("%d", c.Queue), c.Worker, c.State})
	}
	m.mux.SetRows(mr)
	qr := []table.Row{}
	for _, q := range st.Quota {
		lim := kfmt(q.Limit)
		if q.Limit == 0 {
			lim = "∞"
		}
		qr = append(qr, table.Row{q.Resource, kfmt(q.Used), lim, q.State})
	}
	m.quota.SetRows(qr)
}

func (m model) selected() *Worker {
	if m.st == nil || len(m.st.Workers) == 0 {
		return nil
	}
	i := m.agents.Cursor()
	if i < 0 || i >= len(m.st.Workers) {
		return nil
	}
	return &m.st.Workers[i]
}

func (m *model) setFocus(f int) {
	m.focus = f
	m.agents.Blur()
	m.mux.Blur()
	m.quota.Blur()
	switch f {
	case 0:
		m.agents.Focus()
	case 1:
		m.mux.Focus()
	case 2:
		m.quota.Focus()
	}
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width, m.height = msg.Width, msg.Height
		return m, nil
	case tickMsg:
		cmds := []tea.Cmd{tick()}
		if !m.polling {
			m.polling = true
			cmds = append(cmds, m.pollCmd())
		}
		return m, tea.Batch(cmds...)
	case statusMsg:
		m.polling = false
		if msg.err != nil {
			m.online = false
			m.lastErr = msg.err.Error()
		} else {
			m.online = true
			m.applyStatus(msg.st)
		}
		return m, nil
	case actionMsg:
		if msg.err != nil {
			m.flash = "✗ " + msg.err.Error()
		} else {
			m.flash = "✓ " + msg.text
		}
		return m, m.pollCmd()
	case tea.KeyMsg:
		switch m.mode {
		case modeMorph:
			return m.updateMorph(msg)
		case modeSubmit, modePrompt:
			return m.updateInput(msg)
		}
		return m.updateNormal(msg)
	}
	return m, nil
}

func (m model) updateNormal(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	w := m.selected()
	switch k.String() {
	case "q", "ctrl+c":
		return m, tea.Quit
	case "tab":
		m.setFocus((m.focus + 1) % 3)
		return m, nil
	case "shift+tab":
		m.setFocus((m.focus + 2) % 3)
		return m, nil
	case "m":
		if w != nil && m.st != nil {
			m.mode = modeMorph
			for i, r := range m.st.Roles {
				if r == w.Role {
					m.roleIdx = i
				}
			}
		}
		return m, nil
	case "p":
		if w == nil {
			return m, nil
		}
		act := "park"
		if w.Status == "parked" || w.Status == "checkpointed" {
			act = "unpark"
		}
		return m, m.actionCmd(map[string]any{"action": act, "worker_id": w.ID})
	case "s":
		if w != nil {
			return m, m.actionCmd(map[string]any{"action": "checkpoint", "worker_id": w.ID})
		}
	case "a":
		if w != nil {
			return m, m.actionCmd(map[string]any{"type": "abort", "worker": w.ID})
		}
	case "n":
		m.mode = modeSubmit
		m.input.Placeholder = "goal for the swarm (planner decomposes → MUX fan-out)"
		m.input.SetValue("")
		return m, m.input.Focus()
	case "i":
		if w != nil {
			m.mode = modePrompt
			m.input.Placeholder = "prompt " + w.ID + " directly (queued as follow-up if busy)"
			m.input.SetValue("")
			return m, m.input.Focus()
		}
	}
	var cmd tea.Cmd
	switch m.focus {
	case 0:
		m.agents, cmd = m.agents.Update(k)
	case 1:
		m.mux, cmd = m.mux.Update(k)
	case 2:
		m.quota, cmd = m.quota.Update(k)
	}
	return m, cmd
}

func (m model) updateMorph(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	roles := m.st.Roles
	switch k.String() {
	case "esc", "q":
		m.mode = modeNormal
	case "up", "k":
		if m.roleIdx > 0 {
			m.roleIdx--
		}
	case "down", "j":
		if m.roleIdx < len(roles)-1 {
			m.roleIdx++
		}
	case "enter":
		m.mode = modeNormal
		if w := m.selected(); w != nil && m.roleIdx < len(roles) {
			return m, m.actionCmd(map[string]any{"action": "morph", "worker_id": w.ID,
				"new_role": roles[m.roleIdx], "keep_context": true})
		}
	}
	return m, nil
}

func (m model) updateInput(k tea.KeyMsg) (tea.Model, tea.Cmd) {
	switch k.String() {
	case "esc":
		m.mode = modeNormal
		m.input.Blur()
		return m, nil
	case "enter":
		v := strings.TrimSpace(m.input.Value())
		md := m.mode
		m.mode = modeNormal
		m.input.Blur()
		if v == "" {
			return m, nil
		}
		if md == modeSubmit {
			return m, m.actionCmd(map[string]any{"action": "submit", "goal": v})
		}
		if w := m.selected(); w != nil {
			return m, m.actionCmd(map[string]any{"type": "prompt", "worker": w.ID, "message": v,
				"streamingBehavior": "followUp"})
		}
		return m, nil
	}
	var cmd tea.Cmd
	m.input, cmd = m.input.Update(k)
	return m, cmd
}

// ------------------------------------------------------------------ view

func stateColor(s string) lipgloss.Style {
	switch {
	case strings.Contains(s, "exhausted"), strings.Contains(s, "ERROR"), strings.HasPrefix(s, "✗"):
		return lipgloss.NewStyle().Foreground(bad)
	case strings.Contains(s, "near"), strings.Contains(s, "backpressure"), strings.Contains(s, "parked"):
		return lipgloss.NewStyle().Foreground(warn)
	}
	return lipgloss.NewStyle().Foreground(good)
}

func (m model) box(i int, title, body string) string {
	st := boxStyle
	if m.focus == i {
		st = focusBox
	}
	return st.Render(lipgloss.JoinVertical(lipgloss.Left, hdr.Render(title), body))
}

func (m model) View() string {
	header := hdr.Render("MUX HOST-DRIVEN ARCHITECTURE — SPATIAL OPERATIONS CENTER") + muted.Render("  v"+version)
	if !m.online {
		return lipgloss.JoinVertical(lipgloss.Left, header, "",
			lipgloss.NewStyle().Foreground(bad).Render("● HOST OFFLINE — "+m.sock),
			muted.Render("  "+m.lastErr), "",
			"  start it:  python3 host_orchestrator.py serve   (or: bash run.sh)",
			"", muted.Render("q: quit • retrying every 500ms"))
	}
	st := m.st
	hostLine := lipgloss.NewStyle().Foreground(good).Render(fmt.Sprintf("● %s • MUX depth %d • up %ds",
		st.Host, st.MuxDepth, st.Uptime))

	agents := m.box(0, "1 HOST ORCHESTRATOR → MUX ROUTING TO WORKERS", m.agents.View())
	muxB := m.box(1, "2 MUX ROUTER • CH0 In | CH1 Ctx | CH2 Tool", m.mux.View())
	quota := m.box(2, "3 CHECKPOINTING & QUOTA", m.quota.View())

	// detail panel: selected worker todos + git log
	var det []string
	if w := m.selected(); w != nil {
		cur := "idle"
		if w.Current != nil {
			cur = *w.Current
		}
		det = append(det, hdr.Render(fmt.Sprintf("%s • %s", w.ID, w.Role)), muted.Render("task: "+cur))
		for _, t := range w.Todos {
			mark := map[string]string{"completed": "[x]", "in_progress": "[~]"}[t.Status]
			if mark == "" {
				mark = "[ ]"
			}
			det = append(det, mark+" "+strings.Join(strings.Fields(t.Content), " "))
		}
	}
	det = append(det, "", hdr.Render("GIT"))
	for _, g := range st.GitLog {
		det = append(det, truncate(g, 44))
	}
	dw := 46
	if m.width > 0 {
		dw = m.width - lipgloss.Width(agents) - 4
	}
	if dw < 16 {
		dw = 16
	}
	for i := range det {
		det[i] = truncate(det[i], dw)
	}
	detail := boxStyle.Width(dw + 2).Render(strings.Join(det, "\n"))

	evW := 100
	if m.width > 20 {
		evW = m.width - 6
	}
	top := lipgloss.JoinVertical(lipgloss.Left, header, hostLine,
		lipgloss.JoinHorizontal(lipgloss.Top, agents, detail),
		lipgloss.JoinHorizontal(lipgloss.Top, muxB, quota))
	room := 14
	if m.height > 0 {
		room = m.height - lipgloss.Height(top) - 5 // events border+title, flash, footer
	}
	evs := st.Events
	if room < 1 {
		room = 1
	}
	if len(evs) > room {
		evs = evs[len(evs)-room:]
	}
	var ev []string
	for _, e := range evs {
		ev = append(ev, stateColor(e).Render(truncate(e, evW)))
	}
	events := boxStyle.Render(lipgloss.JoinVertical(lipgloss.Left, hdr.Render("EVENTS"), strings.Join(ev, "\n")))

	var overlay string
	switch m.mode {
	case modeMorph:
		lines := []string{hdr.Render("MORPH " + m.selected().ID + " → role (enter, esc)")}
		for i, r := range st.Roles {
			p := "  "
			if i == m.roleIdx {
				p = "▸ "
			}
			lines = append(lines, p+r)
		}
		overlay = focusBox.Render(strings.Join(lines, "\n"))
	case modeSubmit, modePrompt:
		t := "NEW GOAL → planner"
		if m.mode == modePrompt {
			t = "PROMPT " + m.selected().ID
		}
		overlay = focusBox.Render(hdr.Render(t+" (enter, esc)") + "\n" + m.input.View())
	}

	footer := muted.Render("tab focus • ↑↓ select • m morph • p park/unpark • s checkpoint • n new goal • i prompt • a abort • q quit")
	flash := ""
	if m.flash != "" {
		flash = stateColor(m.flash).Render(truncate(m.flash, evW))
	}
	bottom := events
	if overlay != "" {
		bottom = overlay // overlay replaces the event log so the frame never exceeds the screen
	}
	parts := []string{top, bottom, flash, footer}
	return lipgloss.JoinVertical(lipgloss.Left, parts...)
}

func truncate(s string, n int) string {
	r := []rune(s)
	if len(r) <= n {
		return s
	}
	return string(r[:n-1]) + "…"
}

func main() {
	sock := flag.String("sock", envOr("MUX_SOCK", "/tmp/mux_host.sock"), "host unix socket")
	showVer := flag.Bool("version", false, "print version")
	once := flag.Bool("status", false, "print one status snapshot as JSON and exit (no TUI)")
	flag.Parse()
	if *showVer {
		fmt.Println(version)
		return
	}
	if *once {
		_, raw, err := request(*sock, map[string]any{"action": "status"}, 2*time.Second)
		if err != nil {
			fmt.Fprintln(os.Stderr, "host offline:", err)
			os.Exit(1)
		}
		os.Stdout.Write(raw)
		return
	}
	p := tea.NewProgram(initialModel(*sock), tea.WithAltScreen())
	if _, err := p.Run(); err != nil {
		fmt.Fprintf(os.Stderr, "cockpit: %v\n", err)
		os.Exit(1)
	}
}

func envOr(k, d string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return d
}
