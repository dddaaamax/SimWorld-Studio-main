import React, { useState, useEffect, useRef, useCallback, useMemo } from "react";
import ReactDOM from "react-dom";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import PixelStreamPlayer from "./PixelStreamPlayer.jsx";

// ─── SSE Status Stream — Split Contexts (P2-1 fix) ─────────────────────────
// Four fine-grained contexts so consumers only re-render when their slice changes.
// Old single PollContext caused ALL consumers to re-render on every 3s SSE push.

const AgentsContext  = React.createContext({ agents: [], sessions: [], activities: {} });
const SceneContext   = React.createContext({ objects: [], environment: { ready: false }, round: 0 });
const ChatLogContext = React.createContext([]);
const StatusContext  = React.createContext({ pieActive: false, health: null });
const MetricsContext = React.createContext({ series: {}, sceneCollisions: [], sampledAt: 0, intervalMs: 5000 });

// Stale agent context — agents last seen + any sync errors
const SyncContext = React.createContext({ staleAgents: new Set(), syncError: null, sseOk: true });

// Legacy combined context kept for usePoll() callers that haven't migrated yet
const PollContext = React.createContext({
  context: { agents: [], objects: [], environment: { ready: false }, round: 0 },
  sessions: [], activities: {}, chatLog: [], pieActive: false, health: null,
});

// Threshold: agent not seen in UE for > 20s is "stale"
const STALE_AGENT_MS = 20_000;

function PollProvider({ children }) {
  const [agents,   setAgents]   = useState({ agents: [], sessions: [], activities: {} });
  const [scene,    setScene]    = useState({ objects: [], environment: { ready: false }, round: 0 });
  const [chatLog,  setChatLog]  = useState([]);
  const [status,   setStatus]   = useState({ pieActive: false, health: null });
  const [sync,     setSync]     = useState({ staleAgents: new Set(), syncError: null, sseOk: true });
  const [metrics,  setMetrics]  = useState({ series: {}, sceneCollisions: [], sampledAt: 0, intervalMs: 5000 });

  const agentLastSeen  = useRef(new Map()); // agentName → timestamp
  const agentMissCnt   = useRef(new Map()); // agentName → consecutive miss count
  const legacyRef = useRef({ context: { agents:[], objects:[], environment:{ready:false}, round:0 }, sessions:[], activities:{}, chatLog:[], pieActive:false, health:null });

  useEffect(() => {
    const token = sessionStorage.getItem("sw_session_token") || "";
    const url   = token ? `${API_BASE}/events?token=${token}` : `${API_BASE}/events`;
    let es = new EventSource(url);
    let reconnectTimer = null;

    const reconnect = () => {
      es.close();
      reconnectTimer = setTimeout(() => {
        es = new EventSource(url);
        es.onmessage = onMessage;
        es.onerror   = onError;
      }, 3000);
    };

    function onMessage(evt) {
      try {
        const d = JSON.parse(evt.data);
        setSync(prev => prev.sseOk ? prev : { ...prev, sseOk: true, syncError: null });

        // Track agent last-seen timestamps
        const now = Date.now();
        const liveNames = new Set();
        (d.sessions || d.context?.agents || []).forEach(a => {
          const name = a.agentName || a.name;
          if (name) {
            agentLastSeen.current.set(name, now);
            agentMissCnt.current.delete(name); // reset on seen
            liveNames.add(name);
          }
        });

        // Stale detection: require 3+ consecutive missed pushes (not just 1)
        // to avoid false positives from SSE network jitter
        const stale = new Set();
        for (const [name, ts] of agentLastSeen.current) {
          if (!liveNames.has(name)) {
            const misses = (agentMissCnt.current.get(name) || 0) + 1;
            agentMissCnt.current.set(name, misses);
            if (misses >= 3 && now - ts > STALE_AGENT_MS) stale.add(name);
            if (now - ts > 300_000) {
              agentLastSeen.current.delete(name);
              agentMissCnt.current.delete(name);
            }
          }
        }
        setSync(prev => {
          const same = prev.staleAgents.size === stale.size && [...stale].every(n => prev.staleAgents.has(n));
          return same ? prev : { ...prev, staleAgents: stale };
        });

        // Agents slice — lightweight comparison (names + count, avoid full stringify)
        const nextSessions = d.sessions || [];
        const nextAgentList = d.context?.agents || [];
        const nextActivities = d.activities || {};
        setAgents(prev => {
          const prevSess = prev.sessions || [];
          const sameCount = prevSess.length === nextSessions.length;
          // Check key agent state fields to detect actual changes
          const sessKey = s => `${s.agentName}:${s.status}:${s.collisionCount}:${Math.round((s.location?.[0]||0)/10)}:${s.currentAction||''}`;
          const sameKey  = sameCount && nextSessions.every((s,i) => sessKey(s) === sessKey(prevSess[i]));
          // Compare activity content (not just key names) to detect new turns
          const actKey = acts => Object.entries(acts||{}).map(([k,v])=>`${k}:${(v||[]).length}:${(v||[])[v?.length-1]?.timestamp||0}`).join('|');
          const sameActs = actKey(nextActivities) === actKey(prev.activities);
          if (sameKey && sameActs && (prev.agents||[]).length === nextAgentList.length) return prev;
          return { agents: nextAgentList, sessions: nextSessions, activities: nextActivities };
        });

        // Scene slice — only track count + env.ready (objects list can be 30k items)
        const nextEnv   = d.context?.environment || { ready: false };
        const nextObjs  = d.context?.objects     || [];
        const nextRound = d.context?.round       || 0;
        setScene(prev => {
          if (prev.objects.length === nextObjs.length &&
              prev.environment?.ready === nextEnv.ready &&
              prev.round === nextRound) return prev;
          return { objects: nextObjs, environment: nextEnv, round: nextRound };
        });

        // ChatLog — append new only; stable dedup key includes content slice
        if (Array.isArray(d.chatLog) && d.chatLog.length > 0) {
          setChatLog(prev => {
            const msgKey = m => `${m.from}|${m.timestamp}|${(m.text||'').slice(0,20)}`;
            const existing = new Set(prev.map(msgKey));
            const news = d.chatLog.filter(m => !existing.has(msgKey(m)));
            return news.length > 0 ? [...prev, ...news].slice(-200) : prev;
          });
        }

        // Status — only compare the fields we care about
        const nextHealth = d.health || null;
        const nextPie    = !!d.pieActive;
        setStatus(prev => {
          if (prev.pieActive === nextPie &&
              prev.health?.ueConnected  === nextHealth?.ueConnected &&
              prev.health?.mcpConnected === nextHealth?.mcpConnected) return prev;
          return { pieActive: nextPie, health: nextHealth };
        });

        // Metrics time-series (from MetricsHub, sampled every 5s)
        if (d.metrics && d.metrics.sampledAt !== undefined) {
          setMetrics(prev =>
            prev.sampledAt === d.metrics.sampledAt ? prev : d.metrics
          );
        }

        legacyRef.current = d;
      } catch (e) {
        setSync(prev => ({ ...prev, syncError: "SSE parse error: " + e.message }));
      }
    }

    function onError() {
      setSync(prev => ({ ...prev, sseOk: false, syncError: "SSE connection lost — reconnecting…" }));
      reconnect();
    }

    es.onmessage = onMessage;
    es.onerror   = onError;
    return () => { es.close(); if (reconnectTimer) clearTimeout(reconnectTimer); };
  }, []);

  // Legacy combined context value — stable object so usePoll() consumers
  // still work but don't get extra re-renders from the ref itself
  const legacyValue = useMemo(() => ({
    get context()    { return legacyRef.current.context    || {}; },
    get sessions()   { return legacyRef.current.sessions   || []; },
    get activities() { return legacyRef.current.activities || {}; },
    get chatLog()    { return legacyRef.current.chatLog    || []; },
    get pieActive()  { return legacyRef.current.pieActive  || false; },
    get health()     { return legacyRef.current.health     || null; },
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), []); // intentionally stable — consumers that need reactivity should use fine-grained contexts

  return (
    <AgentsContext.Provider  value={agents}>
    <SceneContext.Provider   value={scene}>
    <ChatLogContext.Provider value={chatLog}>
    <StatusContext.Provider  value={status}>
    <MetricsContext.Provider value={metrics}>
    <SyncContext.Provider    value={sync}>
    <PollContext.Provider    value={legacyValue}>
      {children}
    </PollContext.Provider>
    </SyncContext.Provider>
    </MetricsContext.Provider>
    </StatusContext.Provider>
    </ChatLogContext.Provider>
    </SceneContext.Provider>
    </AgentsContext.Provider>
  );
}

// Fine-grained hooks — prefer these over usePoll() for new code
function useAgents()  { return React.useContext(AgentsContext); }
function useScene()   { return React.useContext(SceneContext); }
function useChatLog() { return React.useContext(ChatLogContext); }
function useStatus()  { return React.useContext(StatusContext); }
function useSync()    { return React.useContext(SyncContext); }
function useMetrics() { return React.useContext(MetricsContext); }

// Legacy hook — works but causes full re-render on every SSE push
function usePoll() { return React.useContext(PollContext); }

// ─── Inline SVG Icons (flat colorful cartoon style) ─────────────────────────

function SvgIcon({ children, size = "1em", style, ...props }) {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" width={size} height={size} fill="none" style={{ display: "inline-block", verticalAlign: "middle", flexShrink: 0, ...style }} {...props}>
      {children}
    </svg>
  );
}

const ICONS = {
  // ── feather/outline monochrome icons — all use currentColor ──
  clipboard: (s) => <SvgIcon size={s}><path d="M16 4h2a2 2 0 012 2v14a2 2 0 01-2 2H6a2 2 0 01-2-2V6a2 2 0 012-2h2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><rect x="8" y="2" width="8" height="4" rx="1" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="9" y1="12" x2="15" y2="12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><line x1="9" y1="16" x2="13" y2="16" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  search: (s) => <SvgIcon size={s}><circle cx="11" cy="11" r="8" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="21" y1="21" x2="16.65" y2="16.65" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></SvgIcon>,
  plus: (s) => <SvgIcon size={s}><line x1="12" y1="5" x2="12" y2="19" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/><line x1="5" y1="12" x2="19" y2="12" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></SvgIcon>,
  trash: (s) => <SvgIcon size={s}><polyline points="3 6 5 6 21 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><path d="M19 6l-1 14H6L5 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M10 11v6M14 11v6" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><path d="M9 6V4h6v2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  transform: (s) => <SvgIcon size={s}><polyline points="5 9 2 12 5 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="19 9 22 12 19 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="2" y1="12" x2="22" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  document: (s) => <SvgIcon size={s}><path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="14 2 14 8 20 8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><line x1="8" y1="13" x2="16" y2="13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><line x1="8" y1="17" x2="13" y2="17" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  video: (s) => <SvgIcon size={s}><polygon points="23 7 16 12 23 17 23 7" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><rect x="1" y="5" width="15" height="14" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/></SvgIcon>,
  camera: (s) => <SvgIcon size={s}><path d="M23 19a2 2 0 01-2 2H3a2 2 0 01-2-2V8a2 2 0 012-2h4l2-3h6l2 3h4a2 2 0 012 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><circle cx="12" cy="13" r="4" stroke="currentColor" strokeWidth="2" fill="none"/></SvgIcon>,
  python: (s) => <SvgIcon size={s}><path d="M12 2c-3.5 0-6 1.5-6 4v2h6M6 8H4C2 8 2 10 2 12s0 4 2 4h2v-2c0-2.5 2-4 4-4h4c2 0 4-1.5 4-4V6c0-2.5-2.5-4-6-4z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none"/><path d="M12 22c3.5 0 6-1.5 6-4v-2h-6M18 16h2c2 0 2-2 2-4s0-4-2-4h-2v2c0 2.5-2 4-4 4H10c-2 0-4 1.5-4 4v2c0 2.5 2.5 4 6 4z" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" fill="none"/><circle cx="9" cy="5.5" r="1" fill="currentColor"/><circle cx="15" cy="18.5" r="1" fill="currentColor"/></SvgIcon>,
  wrench: (s) => <SvgIcon size={s}><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  gear: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="2" fill="none"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09a1.65 1.65 0 00-1-1.51 1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06a1.65 1.65 0 00.33-1.82 1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09a1.65 1.65 0 001.51-1 1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06a1.65 1.65 0 001.82.33h0a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06a1.65 1.65 0 00-.33 1.82v0a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z" stroke="currentColor" strokeWidth="2" fill="none"/></SvgIcon>,
  palette: (s) => <SvgIcon size={s}><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.83 0 1.5-.67 1.5-1.5 0-.39-.15-.74-.39-1.01A1.5 1.5 0 0114 18h1.5c3.04 0 5.5-2.46 5.5-5.5C21 6.81 17.05 2 12 2z" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="8.5" cy="10.5" r="1.5" fill="currentColor"/><circle cx="12.5" cy="7.5" r="1.5" fill="currentColor"/><circle cx="16.5" cy="10.5" r="1.5" fill="currentColor"/><circle cx="15.5" cy="14.5" r="1.5" fill="currentColor"/></SvgIcon>,
  plug: (s) => <SvgIcon size={s}><path d="M7 2v8M17 2v8M6 10h12v2a6 6 0 01-6 6v0a6 6 0 01-6-6v-2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="12" y1="18" x2="12" y2="22" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  eye: (s) => <SvgIcon size={s}><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><circle cx="12" cy="12" r="3" stroke="currentColor" strokeWidth="2" fill="none"/></SvgIcon>,
  map: (s) => <SvgIcon size={s}><polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="8" y1="2" x2="8" y2="18" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><line x1="16" y1="6" x2="16" y2="22" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  building: (s) => <SvgIcon size={s}><path d="M3 2h18v20H3zM9 2v20M15 2v20M3 8h18M3 14h18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  tree: (s) => <SvgIcon size={s}><path d="M12 2l7 12H5L12 2z" stroke="currentColor" strokeWidth="2" strokeLinejoin="round" fill="none"/><path d="M12 8l5 9H7l5-9z" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" fill="none"/><line x1="12" y1="18" x2="12" y2="22" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/></SvgIcon>,
  car: (s) => <SvgIcon size={s}><path d="M5 12l2-6h10l2 6" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><rect x="1" y="12" width="22" height="7" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="6" cy="19" r="2" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="18" cy="19" r="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="1" y1="15" x2="23" y2="15" stroke="currentColor" strokeWidth="1.5"/></SvgIcon>,
  hydrant: (s) => <SvgIcon size={s}><rect x="7" y="11" width="10" height="10" rx="1" stroke="currentColor" strokeWidth="2" fill="none"/><rect x="9" y="5" width="6" height="6" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="5" y1="14" x2="7" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="17" y1="14" x2="19" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><circle cx="12" cy="7.5" r="1" fill="currentColor"/></SvgIcon>,
  road: (s) => <SvgIcon size={s}><path d="M5 22L9 2h6l4 20H5z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="12" y1="4" x2="12" y2="7" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="12" y1="10" x2="12" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="12" y1="17" x2="12" y2="20" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  cube: (s) => <SvgIcon size={s}><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="3.27 6.96 12 12.01 20.73 6.96" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="22.08" x2="12" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  hammer: (s) => <SvgIcon size={s}><path d="M15 12l-8.5 8.5c-.83.83-2.17.83-3 0s-.83-2.17 0-3L12 9" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M17.64 15L22 10.64M20.91 11.7l-1.25-1.25c-.6-.6-.93-1.4-.93-2.25v-.86L16.01 4.6a5 5 0 00-3-2.57L9 6.86a5 5 0 002.57 3L13 11" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  box: (s) => <SvgIcon size={s}><path d="M21 16V8a2 2 0 00-1-1.73l-7-4a2 2 0 00-2 0l-7 4A2 2 0 003 8v8a2 2 0 001 1.73l7 4a2 2 0 002 0l7-4A2 2 0 0021 16z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="3.27 6.96 12 12.01 20.73 6.96" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="22.08" x2="12" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><polyline points="7.5 4.21 12 6.81 16.5 4.21" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/></SvgIcon>,
  chat: (s) => <SvgIcon size={s}><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  robot: (s) => <SvgIcon size={s}><rect x="4" y="7" width="16" height="13" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><rect x="7" y="11" width="3" height="3" rx=".5" stroke="currentColor" strokeWidth="1.5" fill="none"/><rect x="14" y="11" width="3" height="3" rx=".5" stroke="currentColor" strokeWidth="1.5" fill="none"/><line x1="12" y1="4" x2="12" y2="7" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><circle cx="12" cy="3" r="1.5" stroke="currentColor" strokeWidth="1.5" fill="none"/><line x1="1" y1="14" x2="4" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="20" y1="14" x2="23" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="9" y1="17" x2="15" y2="17" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  swords: (s) => <SvgIcon size={s}><polyline points="14.5 17.5 3 6 3 3 6 3 17.5 14.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="13" y1="19" x2="19" y2="13" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><polyline points="20 16 20 20 16 20" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="15" y1="4" x2="4" y2="15" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><polyline points="8 4 4 4 4 8" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  frame: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="8.5" cy="8.5" r="1.5" fill="currentColor"/><polyline points="21 15 16 10 5 21" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  tools: (s) => <SvgIcon size={s}><path d="M14.7 6.3a1 1 0 000 1.4l1.6 1.6a1 1 0 001.4 0l3.77-3.77a6 6 0 01-7.94 7.94l-6.91 6.91a2.12 2.12 0 01-3-3l6.91-6.91a6 6 0 017.94-7.94l-3.76 3.76z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  trophy: (s) => <SvgIcon size={s}><path d="M6 9H4.5a2.5 2.5 0 010-5H6M18 9h1.5a2.5 2.5 0 000-5H18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M7 4h10v7a5 5 0 01-10 0V4z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="12" y1="16" x2="12" y2="20" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><rect x="7" y="19" width="10" height="2.5" rx="1" stroke="currentColor" strokeWidth="1.5" fill="none"/></SvgIcon>,
  gamepad: (s) => <SvgIcon size={s}><rect x="2" y="6" width="20" height="12" rx="5" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="6" y1="12" x2="10" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="8" y1="10" x2="8" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><circle cx="16" cy="10" r="1" fill="currentColor"/><circle cx="18" cy="12" r="1" fill="currentColor"/><circle cx="16" cy="14" r="1" fill="currentColor"/><circle cx="14" cy="12" r="1" fill="currentColor"/></SvgIcon>,
  mouse: (s) => <SvgIcon size={s}><rect x="6" y="3" width="12" height="19" rx="6" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="8" x2="12" y2="11" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  keyboard: (s) => <SvgIcon size={s}><rect x="2" y="6" width="20" height="13" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="6" y1="10" x2="6.01" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="10" y1="10" x2="10.01" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="14" y1="10" x2="14.01" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="18" y1="10" x2="18.01" y2="10" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="8" y1="14" x2="16" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="6" y1="14" x2="6.01" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="18" y1="14" x2="18.01" y2="14" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  liveCircle: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="12" cy="12" r="4" fill="currentColor"/></SvgIcon>,
  warning: (s) => <SvgIcon size={s}><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><line x1="12" y1="9" x2="12" y2="13" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="12" y1="17" x2="12.01" y2="17" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/></SvgIcon>,
  gold: (s) => <SvgIcon size={s}><circle cx="12" cy="9" r="7" stroke="currentColor" strokeWidth="2" fill="none"/><text x="12" y="13" textAnchor="middle" fontSize="9" fontWeight="800" fill="currentColor">1</text><path d="M6 17h12l-1 4H7l-1-4z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/></SvgIcon>,
  silver: (s) => <SvgIcon size={s}><circle cx="12" cy="9" r="7" stroke="currentColor" strokeWidth="2" fill="none"/><text x="12" y="13" textAnchor="middle" fontSize="9" fontWeight="800" fill="currentColor">2</text><path d="M6 17h12l-1 4H7l-1-4z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/></SvgIcon>,
  bronze: (s) => <SvgIcon size={s}><circle cx="12" cy="9" r="7" stroke="currentColor" strokeWidth="2" fill="none"/><text x="12" y="13" textAnchor="middle" fontSize="9" fontWeight="800" fill="currentColor">3</text><path d="M6 17h12l-1 4H7l-1-4z" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" fill="none"/></SvgIcon>,
  book: (s) => <SvgIcon size={s}><path d="M4 19.5A2.5 2.5 0 016.5 17H20" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><line x1="9" y1="8" x2="15" y2="8" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/><line x1="9" y1="12" x2="13" y2="12" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/></SvgIcon>,
  brain: (s) => <SvgIcon size={s}><path d="M9.5 2A2.5 2.5 0 007 4.5 2.5 2.5 0 004.5 7 2.5 2.5 0 002 9.5 2.5 2.5 0 004.5 12 2.5 2.5 0 007 14.5 2.5 2.5 0 009.5 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M14.5 2A2.5 2.5 0 0117 4.5 2.5 2.5 0 0119.5 7 2.5 2.5 0 0122 9.5 2.5 2.5 0 0119.5 12 2.5 2.5 0 0117 14.5 2.5 2.5 0 0114.5 17" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><path d="M9 17v3a2 2 0 002 2h2a2 2 0 002-2v-3M9.5 2h5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  close: (s) => <SvgIcon size={s}><line x1="18" y1="6" x2="6" y2="18" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/><line x1="6" y1="6" x2="18" y2="18" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"/></SvgIcon>,
  check: (s) => <SvgIcon size={s}><polyline points="20 6 9 17 4 12" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  chartBar: (s) => <SvgIcon size={s}><line x1="18" y1="20" x2="18" y2="10" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><line x1="12" y1="20" x2="12" y2="4" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><line x1="6" y1="20" x2="6" y2="14" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><line x1="3" y1="20" x2="21" y2="20" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  layout: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="3" y1="9" x2="21" y2="9" stroke="currentColor" strokeWidth="2"/><line x1="9" y1="21" x2="9" y2="9" stroke="currentColor" strokeWidth="2"/></SvgIcon>,
  panelLeft: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="9" y1="3" x2="9" y2="21" stroke="currentColor" strokeWidth="2"/></SvgIcon>,
  panelRight: (s) => <SvgIcon size={s}><rect x="3" y="3" width="18" height="18" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="15" y1="3" x2="15" y2="21" stroke="currentColor" strokeWidth="2"/></SvgIcon>,
  sun: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="4" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="2" x2="12" y2="4" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="12" y1="20" x2="12" y2="22" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="2" y1="12" x2="4" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="20" y1="12" x2="22" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  moon: (s) => <SvgIcon size={s}><path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  lock: (s) => <SvgIcon size={s}><rect x="3" y="11" width="18" height="11" rx="2" stroke="currentColor" strokeWidth="2" fill="none"/><path d="M7 11V7a5 5 0 0110 0v4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  clock: (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" fill="none"/><polyline points="12 6 12 12 16 14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  ghost: (s) => <SvgIcon size={s}><path d="M9 10h.01M15 10h.01" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/><path d="M12 2a7 7 0 017 7v6l-2-1-2 1-2-1-2 1-2-1-2 1V9a7 7 0 017-7z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  maximize: (s) => <SvgIcon size={s}><path d="M8 3H5a2 2 0 00-2 2v3M21 8V5a2 2 0 00-2-2h-3M3 16v3a2 2 0 002 2h3M16 21h3a2 2 0 002-2v-3" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  zap:      (s) => <SvgIcon size={s}><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  refresh:  (s) => <SvgIcon size={s}><polyline points="23 4 23 10 17 10" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><polyline points="1 20 1 14 7 14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/><path d="M3.51 9a9 9 0 0114.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0020.49 15" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
  collision:(s) => <SvgIcon size={s}><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" fill="none"/><line x1="12" y1="8" x2="12" y2="12" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/><line x1="12" y1="16" x2="12.01" y2="16" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round"/></SvgIcon>,
  target:   (s) => <SvgIcon size={s}><circle cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="12" cy="12" r="6" stroke="currentColor" strokeWidth="2" fill="none"/><circle cx="12" cy="12" r="2" fill="currentColor"/></SvgIcon>,
  activity: (s) => <SvgIcon size={s}><polyline points="22 12 18 12 15 21 9 3 6 12 2 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  folder:   (s) => <SvgIcon size={s}><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" fill="none"/></SvgIcon>,
  scan:     (s) => <SvgIcon size={s}><path d="M3 7V5a2 2 0 012-2h2M17 3h2a2 2 0 012 2v2M21 17v2a2 2 0 01-2 2h-2M7 21H5a2 2 0 01-2-2v-2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><line x1="7" y1="12" x2="17" y2="12" stroke="currentColor" strokeWidth="2" strokeLinecap="round"/></SvgIcon>,
  users:    (s) => <SvgIcon size={s}><path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/><circle cx="9" cy="7" r="4" stroke="currentColor" strokeWidth="2" fill="none"/><path d="M23 21v-2a4 4 0 00-3-3.87M16 3.13a4 4 0 010 7.75" stroke="currentColor" strokeWidth="2" strokeLinecap="round" fill="none"/></SvgIcon>,
};

// ─── Shared UI Primitives ─────────────────────────────────────────────────────
// All themed, no hardcoded colors. Use these instead of inline styles.

// Badge / label chip
const BADGE_VARIANTS = {
  blue:   { background: "var(--blue-soft)",        color: "var(--blue)",   border: "1px solid rgba(76,141,255,0.3)" },
  green:  { background: "var(--green-soft)",        color: "var(--green)",  border: "1px solid rgba(53,208,127,0.3)" },
  orange: { background: "var(--orange-soft)",       color: "var(--orange)", border: "1px solid rgba(255,157,66,0.3)"  },
  red:    { background: "rgba(255,95,99,0.12)",     color: "var(--red)",    border: "1px solid rgba(255,95,99,0.3)"   },
  muted:  { background: "var(--panel-2)",           color: "var(--ink-3)",  border: "1px solid var(--line)"           },
  violet: { background: "var(--violet-soft)",       color: "var(--violet)", border: "1px solid rgba(165,110,255,0.3)"},
};

function Badge({ variant = "muted", children, style, dot }) {
  return (
    <span style={{
      display: "inline-flex", alignItems: "center", gap: 4,
      padding: "2px 8px", borderRadius: 6,
      fontSize: 11, fontWeight: 700, whiteSpace: "nowrap",
      ...BADGE_VARIANTS[variant], ...style,
    }}>
      {dot && <span style={{ width: 6, height: 6, borderRadius: "50%", background: "currentColor", flexShrink: 0 }} />}
      {children}
    </span>
  );
}

// Maps skill.source or tool status to the right Badge variant
function SourceBadge({ source, style }) {
  const map = {
    builtin: ["muted",  "builtin"],
    custom:  ["blue",   "custom"],
    learned: ["blue",   "learned"],
  };
  const [v, label] = map[source] || ["muted", source];
  return <Badge variant={v} style={style}>{label}</Badge>;
}

function StatusBadge({ enabled, readOnly, style }) {
  if (readOnly) return <Badge variant="muted" style={style}>Static MCP</Badge>;
  return <Badge variant={enabled ? "green" : "muted"} style={style}>{enabled ? "Enabled" : "Disabled"}</Badge>;
}

// Action button with consistent variants
const BTN_VARIANTS = {
  primary: { background: "var(--blue)",             color: "#fff",           border: "1px solid var(--blue)"              },
  success: { background: "var(--green)",            color: "#fff",           border: "1px solid var(--green)"             },
  danger:  { background: "rgba(255,95,99,0.1)",     color: "var(--red)",     border: "1px solid rgba(255,95,99,0.3)"     },
  enable:  { background: "var(--blue-soft)",        color: "var(--blue)",    border: "1px solid rgba(76,141,255,0.4)"    },
  disable: { background: "var(--panel-2)",          color: "var(--ink-2)",   border: "1px solid var(--line)"             },
  cancel:  { background: "var(--panel-2)",          color: "var(--ink-2)",   border: "1px solid var(--line)"             },
  ghost:   { background: "transparent",             color: "var(--ink-2)",   border: "1px solid var(--line)"             },
};
const BTN_SIZES = {
  xs: { padding: "3px 8px",  fontSize: 11 },
  sm: { padding: "5px 12px", fontSize: 12 },
  md: { padding: "7px 16px", fontSize: 13 },
};

function Btn({ variant = "ghost", size = "sm", onClick, disabled, children, style, ...rest }) {
  return (
    <button onClick={onClick} disabled={disabled} style={{
      display: "inline-flex", alignItems: "center", gap: 5,
      borderRadius: 6, fontFamily: "inherit", fontWeight: 600,
      cursor: disabled ? "wait" : "pointer",
      opacity: disabled ? 0.45 : 1,
      transition: "opacity 0.12s",
      ...BTN_SIZES[size], ...BTN_VARIANTS[variant], ...style,
    }} {...rest}>
      {children}
    </button>
  );
}

// Toggle enable/disable button — picks variant based on current state
function ToggleBtn({ enabled, busy, onClick, style }) {
  return (
    <Btn variant={enabled ? "disable" : "enable"} disabled={busy} onClick={onClick} style={style}>
      {busy ? "Working…" : enabled ? "Disable" : "Enable"}
    </Btn>
  );
}

// Colored tag chip based on TAG_COLORS
function TagChip({ tag }) {
  const color = TAG_COLORS[tag];
  return (
    <span style={{
      fontSize: 11, padding: "2px 6px", borderRadius: 4,
      background: color ? color + "33" : "var(--panel-2)",
      color: color || "var(--ink-3)",
      border: `1px solid ${color ? color + "44" : "var(--line)"}`,
    }}>
      {tag}
    </span>
  );
}

// Modal overlay + panel
function ModalOverlay({ onClose, children, maxWidth = 750, maxHeight = "85vh" }) {
  return (
    <div style={{
      position: "fixed", inset: 0, background: "rgba(0,0,0,0.72)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 9999, backdropFilter: "blur(3px)",
    }} onClick={onClose}>
      <div onClick={e => e.stopPropagation()} style={{
        width: "90%", maxWidth, maxHeight,
        background: "var(--panel)", border: "1px solid var(--line)",
        borderRadius: 10, display: "flex", flexDirection: "column",
        overflow: "hidden", boxShadow: "var(--shadow-pop)",
      }}>
        {children}
      </div>
    </div>
  );
}

function ModalHeader({ title, subtitle, onClose, children }) {
  return (
    <div style={{
      padding: "14px 20px", borderBottom: "1px solid var(--line)",
      display: "flex", alignItems: "center", gap: 10,
      background: "var(--panel-3)", flexShrink: 0,
    }}>
      <div style={{ flex: 1, minWidth: 0 }}>
        {title && <div style={{ fontSize: 16, fontWeight: 700, color: "var(--ink)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{title}</div>}
        {subtitle && <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 3 }}>{subtitle}</div>}
      </div>
      {children}
      {onClose && (
        <button onClick={onClose} style={{
          width: 28, height: 28, display: "flex", alignItems: "center", justifyContent: "center",
          borderRadius: 6, border: "none", background: "transparent",
          color: "var(--ink-3)", cursor: "pointer", flexShrink: 0,
        }}
          onMouseEnter={e => e.currentTarget.style.background = "var(--bg-hover)"}
          onMouseLeave={e => e.currentTarget.style.background = "transparent"}
        >
          {ICONS.close(14)}
        </button>
      )}
    </div>
  );
}

function ModalFooter({ children }) {
  return (
    <div style={{
      padding: "12px 20px", borderTop: "1px solid var(--line)",
      display: "flex", gap: 8, justifyContent: "flex-end",
      background: "var(--panel-3)", flexShrink: 0,
    }}>
      {children}
    </div>
  );
}

// Page-level header bar (Gallery, Skills, Tools, Leaderboard)
function PageHeader({ icon, title, subtitle, action }) {
  return (
    <div style={{
      padding: "14px 24px", borderBottom: "1px solid var(--line)",
      display: "flex", alignItems: "center", gap: 12, flexShrink: 0,
      background: "var(--panel)",
    }}>
      {icon && (
        <span style={{ display: "inline-flex", alignItems: "center", color: "var(--ink-2)", flexShrink: 0 }}>
          {icon}
        </span>
      )}
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 16, fontWeight: 700, color: "var(--ink)" }}>{title}</div>
        {subtitle && <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 2 }}>{subtitle}</div>}
      </div>
      {action}
    </div>
  );
}

// Section eyebrow heading
function Eyebrow({ children, style }) {
  return (
    <div style={{
      fontSize: 11, fontWeight: 700, letterSpacing: "0.07em",
      textTransform: "uppercase", color: "var(--ink-2)", ...style,
    }}>
      {children}
    </div>
  );
}

// Themed form input/textarea wrapper
function Field({ label, children }) {
  return (
    <div>
      {label && <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>{label}</label>}
      {children}
    </div>
  );
}
const inputSx = {
  width: "100%", padding: "7px 12px", fontSize: 13, fontFamily: "inherit",
  background: "var(--bg-tertiary)", border: "1px solid var(--line)",
  borderRadius: 6, color: "var(--ink)", outline: "none",
  cursor: "text", boxSizing: "border-box",
};

// ─── Pipeline + Artifact UI ──────────────────────────────────────────────────

const STUDIO_MODES = [
  { id:"scene",    num:1, label:"Scene Generation", sub:"Create & verify UE5 environments from text/image/edit",     cta:"Generate Scene",   ctaColor:"blue",   icon: s=>ICONS.chat(s)     },
  { id:"task",     num:2, label:"Task Generation",  sub:"Generate PointNav / ObjectNav tasks from verified scenes",  cta:"Generate Tasks",   ctaColor:"green",  icon: s=>ICONS.target(s)  },
  { id:"training", num:3, label:"Agent Training",   sub:"Run embodied agent experiments and collect trajectories",   cta:"Start Training",   ctaColor:"violet", icon: s=>ICONS.robot(s)   },
  { id:"coevolve", num:4, label:"Co-evolution",     sub:"Adaptive curriculum driven by agent-environment feedback",  cta:"Run Co-evolution", ctaColor:"orange", icon: s=>ICONS.refresh(s) },
];

function PipelineStepper({ activeMode, onChange }) {
  return (
    <div className="pipeline-stepper">
      {STUDIO_MODES.map((m, i) => (
        <React.Fragment key={m.id}>
          <button className={`pipeline-tab${activeMode === m.id ? " active" : ""}`}
            onClick={() => onChange(m.id)} title={m.sub}>
            <span className="pipeline-tab-num">{m.num}</span>
            {m.label}
          </button>
          {i < STUDIO_MODES.length - 1 && (
            <svg className="pipeline-arrow" viewBox="0 0 16 16" width="14" height="14" fill="none">
              <polyline points="5,3 11,8 5,13" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          )}
        </React.Fragment>
      ))}
    </div>
  );
}

function ArtifactChain({ artifacts, activeMode, onSelect }) {
  const stages = [
    { id:"scene",    label:"Scene",    placeholder:"No scene yet",    icon: s=>ICONS.cube(s)    },
    { id:"task",     label:"Task Set", placeholder:"No tasks yet",    icon: s=>ICONS.target(s)  },
    { id:"training", label:"Training", placeholder:"No run yet",      icon: s=>ICONS.activity(s)},
    { id:"coevolve", label:"Curriculum",placeholder:"No curriculum",  icon: s=>ICONS.refresh(s) },
  ];
  return (
    <div className="artifact-chain">
      <span style={{ fontSize:10, fontWeight:700, letterSpacing:"0.06em", textTransform:"uppercase", color:"var(--ink-3)", flexShrink:0 }}>Pipeline</span>
      {stages.map((s, i) => {
        const art = artifacts[s.id];
        const isActive = activeMode === s.id;
        return (
          <React.Fragment key={s.id}>
            {i > 0 && <span className="artifact-sep">→</span>}
            <button
              className={`artifact-chip${!art ? " empty" : art ? " ready" : ""}${isActive ? " active" : ""}`}
              onClick={() => art && onSelect(s.id)}
              title={art ? art.name : s.placeholder}
            >
              <span style={{ display:"inline-flex", alignItems:"center" }}>{s.icon(11)}</span>
              {art ? art.name : s.placeholder}
            </button>
          </React.Fragment>
        );
      })}
    </div>
  );
}

// ── Studio mode landing cards ─────────────────────────────────────────────────
function StudioLanding({ activeMode, onSelect, artifacts }) {
  return (
    <div style={{ flex:1, overflow:"auto", background:"var(--bg)" }}>
      <div style={{ padding:"28px 28px 12px", borderBottom:"1px solid var(--line)" }}>
        <div style={{ fontSize:16, fontWeight:700, color:"var(--ink)", marginBottom:4 }}>SimWorld Studio</div>
        <div style={{ fontSize:12, color:"var(--ink-3)" }}>Select a pipeline stage to begin or continue your work.</div>
      </div>
      <div className="mode-landing">
        {STUDIO_MODES.map(m => {
          const art = artifacts[m.id];
          return (
            <div key={m.id} className={`mode-card${activeMode === m.id ? " current" : ""}`}
              onClick={() => onSelect(m.id)}>
              <div className="mode-card-num">{m.num}</div>
              <div>
                <div className="mode-card-title">{m.label}</div>
                <div className="mode-card-sub">{m.sub}</div>
              </div>
              <div className={`mode-card-status${art ? " has-data" : ""}`}>
                {art ? `${ICONS.check(11) } ${art.name}` : "No output yet"}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Task Generation panels ────────────────────────────────────────────────────
function TaskGenPanel({ sessionId }) {
  const [taskType, setTaskType] = React.useState("PointNav");
  const [episodes, setEpisodes] = React.useState("500");
  const [minPath, setMinPath] = React.useState("3");
  const [maxPath, setMaxPath] = React.useState("20");
  const [successR, setSuccessR] = React.useState("0.5");
  const [maxSteps, setMaxSteps] = React.useState("500");
  const [generating, setGenerating] = React.useState(false);

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Task Builder</div>
        <div className="config-row">
          <label>Task type</label>
          <select className="config-select" value={taskType} onChange={e=>setTaskType(e.target.value)}>
            <option>PointNav</option>
            <option>ObjectNav</option>
            <option>Exploration</option>
            <option>Custom</option>
          </select>
        </div>
      </div>

      <div className="config-section" style={{ flex:1, overflow:"auto" }}>
        <div className="config-section-title">Sampling Parameters</div>
        {[
          ["Episodes", episodes, setEpisodes],
          ["Min path (m)", minPath, setMinPath],
          ["Max path (m)", maxPath, setMaxPath],
          ["Success radius (m)", successR, setSuccessR],
          ["Max episode steps", maxSteps, setMaxSteps],
        ].map(([label, val, set]) => (
          <div key={label} className="config-row">
            <label>{label}</label>
            <input className="config-input" value={val} onChange={e=>set(e.target.value)} />
          </div>
        ))}

        {taskType === "PointNav" && (
          <>
            <div className="config-section-title" style={{ marginTop:12 }}>PointNav Options</div>
            {[["Require NavMesh", true],["Filter by path length", true],["Sample reachable pairs", true]].map(([l,v])=>(
              <div key={l} className="config-row">
                <label>{l}</label>
                <span style={{ fontSize:12, color: v?"var(--green)":"var(--ink-3)", fontWeight:600 }}>{v?"On":"Off"}</span>
              </div>
            ))}
          </>
        )}
        {taskType === "ObjectNav" && (
          <>
            <div className="config-section-title" style={{ marginTop:12 }}>ObjectNav Options</div>
            {[["Target category","Any"],["Require reachable","Yes"],["Visible from path","Yes"]].map(([l,v])=>(
              <div key={l} className="config-row">
                <label>{l}</label>
                <span className="config-val">{v}</span>
              </div>
            ))}
          </>
        )}
      </div>

      <div style={{ padding:"10px 14px", borderTop:"1px solid var(--line)", display:"flex", flexDirection:"column", gap:6 }}>
        <button className="primary-cta green" style={{ width:"100%", justifyContent:"center" }}
          onClick={()=>setGenerating(g=>!g)} disabled={generating}>
          {generating ? "Generating…" : `${ICONS.target(13)} Generate Tasks`}
        </button>
        <div style={{ display:"flex", gap:6 }}>
          <Btn variant="ghost" size="sm" style={{ flex:1, justifyContent:"center" }}>Validate Tasks</Btn>
          <Btn variant="ghost" size="sm" style={{ flex:1, justifyContent:"center" }}>Preview Gym API</Btn>
        </div>
      </div>
    </div>
  );
}

function TaskInspectorPanel() {
  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"auto" }}>
      <div className="config-section">
        <div className="config-section-title">Task Set</div>
        {[["Type","PointNav"],["Total episodes","500"],["Valid","486"],["Rejected","14"]].map(([l,v])=>(
          <div key={l} className="status-row"><span>{l}</span><span className="config-val">{v}</span></div>
        ))}
      </div>
      <div className="config-section">
        <div className="config-section-title">Validation</div>
        {[["NavMesh connected","pass"],["Task solvable","pass"],["Goal reachable","pass"],["Collision-free path","pass"]].map(([l,s])=>(
          <div key={l} className="status-row">
            <span style={{ fontSize:12, color:"var(--ink-2)" }}>{l}</span>
            <span className={s==="pass"?"status-pass":"status-fail"}>{s.toUpperCase()}</span>
          </div>
        ))}
      </div>
      <div className="config-section">
        <div className="config-section-title">Selected Episode</div>
        {[["Start","(−420, 130, 200)"],["Goal","(1840, −650, 200)"],["Path length","18.4 m"],["Max steps","500"],["Success radius","0.5 m"]].map(([l,v])=>(
          <div key={l} className="status-row"><span style={{ fontSize:11, color:"var(--ink-3)" }}>{l}</span><span style={{ fontSize:12, color:"var(--ink)", fontFamily:"monospace" }}>{v}</span></div>
        ))}
      </div>
      <div className="config-section">
        <div className="config-section-title">Output Artifact</div>
        <div style={{ padding:"8px 10px", borderRadius:6, border:"1px solid var(--line)", background:"var(--bg-tertiary)", fontSize:12, color:"var(--ink-2)" }}>
          <div style={{ fontWeight:700, color:"var(--ink)", marginBottom:2 }}>TaskSet_PointNav_500</div>
          <div style={{ fontSize:11 }}>486 valid episodes · PointNav · Urban Avenue</div>
        </div>
        <Btn variant="success" size="sm" style={{ width:"100%", justifyContent:"center", marginTop:8 }}>Export Task Set</Btn>
      </div>
    </div>
  );
}

// ── Agent Training config panel ───────────────────────────────────────────────
function TrainingConfigPanel({ sessionId }) {
  const [running, setRunning] = React.useState(false);
  const [obsMode, setObsMode] = React.useState("RGB-D");
  const [method, setMethod] = React.useState("PPO");

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Experiment Setup</div>
        {[["Scene","Urban Avenue v3"],["Task Set","PointNav_500"],["Agent","Qwen2.5-VL-7B"]].map(([l,v])=>(
          <div key={l} className="config-row"><label>{l}</label><span className="config-val" style={{ fontSize:11 }}>{v}</span></div>
        ))}
      </div>

      <div className="config-section" style={{ flex:1, overflow:"auto" }}>
        <div className="config-section-title">Training Config</div>
        <div className="config-row">
          <label>Observation</label>
          <select className="config-select" value={obsMode} onChange={e=>setObsMode(e.target.value)}>
            {["RGB","Depth","RGB-D","Pose","Text"].map(o=><option key={o}>{o}</option>)}
          </select>
        </div>
        <div className="config-row">
          <label>Method</label>
          <select className="config-select" value={method} onChange={e=>setMethod(e.target.value)}>
            {["PPO","DAgger","BC","DDPPO"].map(o=><option key={o}>{o}</option>)}
          </select>
        </div>
        {[["Episode budget","10,000"],["Eval split","20%"],["Memory","Enabled"]].map(([l,v])=>(
          <div key={l} className="config-row"><label>{l}</label><span className="config-val">{v}</span></div>
        ))}

        <div className="config-section-title" style={{ marginTop:12 }}>Live Metrics</div>
        {[["Success Rate","64%"],["SPL","0.42"],["SoftSPL","0.58"],["nDTW","0.71"],["Avg Reward","0.37"]].map(([l,v])=>(
          <div key={l} className="status-row"><span>{l}</span><span className="status-score">{v}</span></div>
        ))}
      </div>

      <div style={{ padding:"10px 14px", borderTop:"1px solid var(--line)", display:"flex", flexDirection:"column", gap:6 }}>
        <button className={`primary-cta ${running?"orange":"violet"}`}
          style={{ width:"100%", justifyContent:"center" }} onClick={()=>setRunning(r=>!r)}>
          {running ? `${ICONS.collision(13)} Pause Training` : `${ICONS.activity(13)} Start Training`}
        </button>
        <div style={{ display:"flex", gap:6 }}>
          <Btn variant="ghost" size="sm" style={{ flex:1, justifyContent:"center" }}>Run Evaluation</Btn>
          <Btn variant="ghost" size="sm" style={{ flex:1, justifyContent:"center" }}>Export</Btn>
        </div>
      </div>
    </div>
  );
}

// ── Co-evolution curriculum builder ──────────────────────────────────────────
function CurriculumBuilderPanel({ sessionId }) {
  const [running, setRunning] = React.useState(false);

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Curriculum Status</div>
        {[["Round","12 / 25"],["Difficulty","Level 4"],["Current SR","64%"],["Next action","Advance to L5"]].map(([l,v])=>(
          <div key={l} className="status-row"><span>{l}</span><span className="config-val">{v}</span></div>
        ))}
      </div>

      <div className="config-section" style={{ flex:1, overflow:"auto" }}>
        <div className="config-section-title">Difficulty Axes</div>
        {[["Path length","12–22 m"],["Heading offset","0–90°"],["Obstacle density","0.20"],["Object clutter","medium"],["Distractors","3"]].map(([l,v])=>(
          <div key={l} className="config-row"><label>{l}</label><span className="config-val">{v}</span></div>
        ))}

        <div className="config-section-title" style={{ marginTop:12 }}>Curriculum Config</div>
        {[["Mastery threshold","70%"],["Episodes per round","500"],["Max rounds","25"],["Advance policy","Consecutive"],["Agent update","Online"]].map(([l,v])=>(
          <div key={l} className="config-row"><label>{l}</label><span className="config-val">{v}</span></div>
        ))}

        <div className="config-section-title" style={{ marginTop:12 }}>SimCoder Adaptation</div>
        <div style={{ display:"flex", flexDirection:"column", gap:4 }}>
          {["Increase obstacle density","Add longer routes","Preserve successful layouts","Oversample sharp-turn failures"].map(s=>(
            <div key={s} style={{ fontSize:11, color:"var(--ink-3)", padding:"3px 0", display:"flex", alignItems:"center", gap:5 }}>
              <span style={{ width:4, height:4, borderRadius:"50%", background:"var(--blue)", flexShrink:0 }}/>
              {s}
            </div>
          ))}
        </div>
      </div>

      <div style={{ padding:"10px 14px", borderTop:"1px solid var(--line)", display:"flex", flexDirection:"column", gap:6 }}>
        <button className={`primary-cta ${running?"orange":"orange"}`}
          style={{ width:"100%", justifyContent:"center" }} onClick={()=>setRunning(r=>!r)}>
          {running ? `${ICONS.collision(13)} Pause` : `${ICONS.refresh(13)} Run Co-evolution`}
        </button>
        <div style={{ display:"flex", gap:6 }}>
          <Btn variant="ghost" size="sm" style={{ flex:1, justifyContent:"center" }}>Evaluate</Btn>
          <Btn variant="ghost" size="sm" style={{ flex:1, justifyContent:"center" }}>Export</Btn>
        </div>
      </div>
    </div>
  );
}

// ── Scene Inspector (right in scene mode) ─────────────────────────────────────
function SceneInspectorPanel({ sessionId, latestScreenshot }) {
  const scene = useScene();
  const actorCount = ((scene.objects || []).length + (scene.agents || []).length) || "—";
  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Scene Summary</div>
        {[["Actors", actorCount], ["Ground size","200 m"], ["Version","v3"]].map(([l,v])=>(
          <div key={l} className="status-row"><span>{l}</span><span className="config-val">{v}</span></div>
        ))}
      </div>

      {/* Live verifier panel */}
      <div style={{ flex:1, overflow:"hidden", display:"flex", flexDirection:"column" }}>
        <div style={{ flex:1, overflow:"hidden" }}>
          <CodingVerifierPanel sessionId={sessionId} latestScreenshot={latestScreenshot} />
        </div>
      </div>
    </div>
  );
}

// ── Round Inspector (right in co-evolve mode) ─────────────────────────────────
function RoundInspectorPanel({ sessionId }) {
  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"hidden" }}>
      <div className="config-section">
        <div className="config-section-title">Round History</div>
        <div style={{ display:"flex", flexDirection:"column", gap:3 }}>
          {[["R1","L0","82%","Advance"],["R2","L1","76%","Advance"],["R3","L2","58%","Hold"],["R4","L2","71%","Advance"],["R5","L3","49%","Hold"],["R12","L4","64%","Active"]].map(([r,l,sr,action])=>(
            <div key={r} style={{ display:"grid", gridTemplateColumns:"2.5rem 2.5rem 2.5rem 1fr", gap:4, padding:"4px 6px", borderRadius:5, background:"var(--bg-tertiary)", fontSize:11, fontFamily:"monospace", alignItems:"center" }}>
              <span style={{ color:"var(--ink-3)" }}>{r}</span>
              <span style={{ color:"var(--ink-2)" }}>{l}</span>
              <span style={{ color:"var(--blue)", fontWeight:700 }}>{sr}</span>
              <span style={{ color: action==="Hold"?"var(--orange)": action==="Active"?"var(--green)":"var(--ink-3)", fontFamily:"inherit", fontSize:11 }}>{action}</span>
            </div>
          ))}
        </div>
      </div>
      <div style={{ flex:1, overflow:"auto" }}>
        <AgentAggregatePanelTabs agents={[]} sessionId={sessionId} />
      </div>
    </div>
  );
}

// ── Library page (Skills + Tools + Assets) ───────────────────────────────────
function LibraryPage({ newlyAddedSkillIds, onMarkSkillSeen, newlyAddedToolIds, onMarkToolSeen }) {
  const [tab, setTab] = React.useState("skills");
  return (
    <div style={{ height:"100%", display:"flex", flexDirection:"column" }}>
      <div style={{ padding:"10px 20px", borderBottom:"1px solid var(--line)", display:"flex", alignItems:"center", gap:8, background:"var(--panel)" }}>
        <span style={{ fontSize:14, fontWeight:700, color:"var(--ink)", marginRight:8 }}>Library</span>
        {[["skills","Skills",ICONS.book],["tools","Tools",ICONS.wrench],["arena","Arena",ICONS.swords]].map(([id,label,icon])=>(
          <button key={id} className={`sw-tab-btn${tab===id?" active":""}`} onClick={()=>setTab(id)}>
            <span style={{ display:"inline-flex", alignItems:"center", gap:4 }}>{icon(13)} {label}</span>
          </button>
        ))}
      </div>
      <div style={{ flex:1, overflow:"hidden" }}>
        {tab==="skills" && <SkillsPage newlyAddedSkillIds={newlyAddedSkillIds} onMarkSkillSeen={onMarkSkillSeen} />}
        {tab==="tools"  && <ToolsPage  newlyAddedToolIds={newlyAddedToolIds}   onMarkToolSeen={onMarkToolSeen}  />}
        {tab==="arena"  && <ArenaPage />}
      </div>
    </div>
  );
}

// ── Results page (Gallery + Leaderboard) ─────────────────────────────────────
function ResultsPage() {
  const [tab, setTab] = React.useState("gallery");
  return (
    <div style={{ height:"100%", display:"flex", flexDirection:"column" }}>
      <div style={{ padding:"10px 20px", borderBottom:"1px solid var(--line)", display:"flex", alignItems:"center", gap:8, background:"var(--panel)" }}>
        <span style={{ fontSize:14, fontWeight:700, color:"var(--ink)", marginRight:8 }}>Results</span>
        {[["gallery","Scenes",ICONS.frame],["leaderboard","Leaderboard",ICONS.trophy]].map(([id,label,icon])=>(
          <button key={id} className={`sw-tab-btn${tab===id?" active":""}`} onClick={()=>setTab(id)}>
            <span style={{ display:"inline-flex", alignItems:"center", gap:4 }}>{icon(13)} {label}</span>
          </button>
        ))}
      </div>
      <div style={{ flex:1, overflow:"hidden" }}>
        {tab==="gallery"     && <GalleryPage />}
        {tab==="leaderboard" && <LeaderboardPage />}
      </div>
    </div>
  );
}

// ─── Constants ───────────────────────────────────────────────────────────────

const API_BASE = "/api";
const EVOLUTION_ARTIFACT_POLL_MS = 5000;
const EVOLUTION_TOAST_MAX = 2;
const EVOLUTION_TOAST_TTL_MS = 5200;

const TOOL_ICONS = {
  get_actors_in_level: ICONS.clipboard,
  find_actors_by_name: ICONS.search,
  spawn_actor: ICONS.plus,
  delete_actor: ICONS.trash,
  set_actor_transform: ICONS.transform,
  get_actor_properties: ICONS.document,
  focus_viewport: ICONS.video,
  take_screenshot: ICONS.camera,
  get_camera_0_view: ICONS.camera,
  execute_python_script: ICONS.python,
  create_blueprint: ICONS.wrench,
  compile_blueprint: ICONS.gear,
  apply_material_to_actor: ICONS.palette,
  initialize: ICONS.plug,
  observe_scene: ICONS.eye,
  get_scene_overview: ICONS.map,
};

const TAG_COLORS = {
  city: "#3b82f6",
  buildings: "#b91c1c",
  props: "#64748b",
  weather: "#ea580c",
  camera: "#7c3aed",
  layout: "#16a34a",
  planning: "#3b82f6",
  spacing: "#f59e0b",
  trees: "#16a34a",
  vehicles: "#ea580c",
  lighting: "#f59e0b",
  atmosphere: "#7c3aed",
  screenshot: "#7c3aed",
  decoration: "#64748b",
  furniture: "#64748b",
  architecture: "#b91c1c",
  environment: "#16a34a",
  viewpoint: "#7c3aed",
  placement: "#f59e0b",
  roads: "#64748b",
  capture: "#7c3aed",
};

const CATEGORY_ICONS = {
  buildings: ICONS.building,
  trees: ICONS.tree,
  vehicles: ICONS.car,
  street_furniture: ICONS.hydrant,
  roads: ICONS.road,
  static_meshes: ICONS.cube,
};

const CATEGORY_COLORS = {
  buildings: "#3b82f6",
  trees: "#16a34a",
  vehicles: "#ea580c",
  street_furniture: "#64748b",
  roads: "#64748b",
  static_meshes: "#94a3b8",
};

const CAMERA_PRESETS = [
  { label: "⬆ Top", title: "Bird's-eye view", args: [0, 0, 5000, -90, 0, 0] },
  { label: "◎ Iso", title: "Isometric overview", args: [3000, -3000, 3000, -35, 45, 0] },
  { label: "▶ Front", title: "Front view (Y-axis)", args: [0, -4000, 1000, 0, 0, 0] },
  { label: "▷ Side", title: "Side view (X-axis)", args: [-4000, 0, 1000, 0, 90, 0] },
];

const QUICK_SUGGESTIONS = [
  "Add more trees",
  "Move buildings further apart",
  "Change to sunset lighting",
  "Take a screenshot from a different angle",
  "Add street furniture",
];

const STATIC_MCP_TOOL_DEFS = [
  {
    id: "spawn_blueprint_actor",
    name: "spawn_blueprint_actor",
    mcpName: "spawn_blueprint_actor",
    enabled: true,
    description:
      "Spawn a SimWorld Blueprint actor (building, tree, vehicle, prop). Use this for all CityDatabase assets. The blueprint_id can be a full path like '/Game/CityDatabase/blueprints/BP_Building_01.BP_Building_01_C', or a shorthand like 'BP_Building_01', 'BP_Tree1', etc. For buildings you can even use just the number like '01' through '06'.",
    paramsSchema: {
      type: "object",
      properties: {
        actor_name: { type: "string", description: "Unique name for this actor (e.g. 'House_01', 'Tree_Left_1')" },
        blueprint_id: {
          type: "string",
          description:
            "Blueprint path or shorthand. Buildings: 'BP_Building_01' to 'BP_Building_06' (ONLY 01-06 available) (or just number). Trees: 'BP_Tree1'-'BP_Tree6'. Vehicles: 'BP_Scooter_01'-'BP_Scooter_04', 'BP_Cart'. Props: 'BP_Hydrant', 'BP_Trash_bin_a', 'BP_Table', etc.",
        },
        location: {
          type: "array",
          items: { type: "number" },
          description:
            "[x, y, z] in UE units (cm). 1m=100 units. Ground is 200m x 200m centered at origin, so keep X and Y between -9500 and 9500. Values outside this range will be clamped to stay on the ground.",
        },
        rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll] in degrees" },
        scale: { type: "array", items: { type: "number" }, description: "[x, y, z] scale multipliers, default [1,1,1]" },
      },
      required: ["actor_name", "blueprint_id", "location"],
    },
  },
  {
    id: "spawn_actor",
    name: "spawn_actor",
    mcpName: "spawn_actor",
    enabled: true,
    description:
      "Spawn a static mesh actor. Use for basic shapes (/Engine/BasicShapes/Cube, Plane, etc.) or SM_ meshes. For SimWorld buildings/trees/props, prefer spawn_blueprint_actor instead.",
    paramsSchema: {
      type: "object",
      properties: {
        name: { type: "string", description: "Unique actor name" },
        static_mesh: {
          type: "string",
          description: "Full mesh path, e.g. '/Engine/BasicShapes/Cube.Cube' or '/Game/CityDatabase/meshes/SM_Road.SM_Road'",
        },
        location: { type: "array", items: { type: "number" }, description: "[x, y, z]" },
        rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll]" },
        scale: { type: "array", items: { type: "number" }, description: "[x, y, z]" },
      },
      required: ["name", "static_mesh", "location"],
    },
  },
  { id: "delete_actor", name: "delete_actor", mcpName: "delete_actor", enabled: true, description: "Delete an actor by its name.", paramsSchema: { type: "object", properties: { name: { type: "string", description: "Actor name to delete" } }, required: ["name"] } },
  { id: "delete_all_spawned", name: "delete_all_spawned", mcpName: "delete_all_spawned", enabled: true, description: "Delete ALL actors spawned in this session. Use to clear the scene before rebuilding.", paramsSchema: { type: "object", properties: {} } },
  { id: "get_actors_in_level", name: "get_actors_in_level", mcpName: "get_actors_in_level", enabled: true, description: "List all actors currently in the UE level.", paramsSchema: { type: "object", properties: {} } },
  { id: "find_actors_by_name", name: "find_actors_by_name", mcpName: "find_actors_by_name", enabled: true, description: "Search for actors whose name matches a pattern.", paramsSchema: { type: "object", properties: { pattern: { type: "string", description: "Name pattern to search" } }, required: ["pattern"] } },
  { id: "set_actor_transform", name: "set_actor_transform", mcpName: "set_actor_transform", enabled: true, description: "Move, rotate, or scale an existing actor.", paramsSchema: { type: "object", properties: { name: { type: "string", description: "Actor name" }, location: { type: "array", items: { type: "number" }, description: "[x, y, z]" }, rotation: { type: "array", items: { type: "number" }, description: "[pitch, yaw, roll]" }, scale: { type: "array", items: { type: "number" }, description: "[x, y, z]" } }, required: ["name"] } },
  { id: "take_screenshot", name: "take_screenshot", mcpName: "take_screenshot", enabled: true, description: "Capture a screenshot of the current UE viewport and save it as PNG.", paramsSchema: { type: "object", properties: { filename: { type: "string", description: "Output filename (optional, auto-generated if omitted)" } } } },
  { id: "execute_python_script", name: "execute_python_script", mcpName: "execute_python_script", enabled: true, description: "Execute arbitrary Unreal Engine Python script. Use for advanced operations not covered by other tools.", paramsSchema: { type: "object", properties: { script: { type: "string", description: "Python code to execute in UE" } }, required: ["script"] } },
  { id: "list_assets", name: "list_assets", mcpName: "list_assets", enabled: true, description: "List available SimWorld assets. Returns buildings, trees, vehicles, street furniture, roads, and static meshes with their paths.", paramsSchema: { type: "object", properties: { category: { type: "string", description: "Optional: 'buildings', 'trees', 'vehicles', 'street_furniture', 'roads', 'static_meshes'. Omit for all." } } } },
  { id: "setup_environment", name: "setup_environment", mcpName: "setup_environment", enabled: true, description: "CALL THIS FIRST before spawning any objects! Sets up the scene environment: directional light (sun), sky atmosphere, sky light, fog, ground plane, and increases view distance. Without this, the scene will be black/empty.", paramsSchema: { type: "object", properties: { ground_size: { type: "number", description: "Ground plane scale (default 200 = 20km x 20km). Use 100 for small scenes, 300 for large cities." }, time_of_day: { type: "string", description: "'morning', 'noon', 'afternoon' (default), 'sunset', or 'night'" } } } },
  { id: "verify_scene", name: "verify_scene", mcpName: "verify_scene", enabled: true, description: "Call a verifier AI to analyze the current scene. Takes a screenshot, gets all actors, then evaluates if placement is correct and matches the original request. Returns structured feedback with status (PASS/NEEDS_IMPROVEMENT/FAIL), issues found, and actionable suggestions. Use this after placing objects to check quality before finishing.", paramsSchema: { type: "object", properties: { original_request: { type: "string", description: "The original scene generation request to verify against (e.g. 'a suburban street with 3 houses and 2 trees')" }, focus_areas: { type: "string", description: "Optional: specific aspects to focus on (e.g. 'check building spacing', 'verify tree placement')" } }, required: [] } },
].sort((a, b) => a.id.localeCompare(b.id));

function buildWelcomeMessage() {
  return {
    id: generateMessageId(),
    role: "assistant",
    content: `Welcome to **SimWorld Studio**! I'm your scene generation agent.

I can build city scenes in Unreal Engine using SimWorld's assets.

Try:
- *"Build a small residential neighborhood with 6 houses and tree-lined streets"*
- *"Create a busy downtown intersection with tall buildings"*
- *"Place a park with trees and benches, set the weather to sunset"*`,
    timestamp: Date.now(),
  };
}

// ─── API Functions ───────────────────────────────────────────────────────────

async function fetchHealth() {
  return (await fetch(`${API_BASE}/health`)).json();
}

async function fetchSkills() {
  return (await fetch(`${API_BASE}/skills`)).json();
}

async function fetchSkillDetails(id) {
  return (await fetch(`${API_BASE}/skills/${id}`)).json();
}

async function createSkill(skill) {
  return (
    await fetch(`${API_BASE}/skills`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(skill),
    })
  ).json();
}

async function deleteSkill(id) {
  await fetch(`${API_BASE}/skills/${id}`, { method: "DELETE" });
}

async function fetchScenes() {
  return (await fetch(`${API_BASE}/scenes`)).json();
}

async function saveScene(scene) {
  return (
    await fetch(`${API_BASE}/scenes`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(scene),
    })
  ).json();
}

async function deleteScene(id) {
  await fetch(`${API_BASE}/scenes/${id}`, { method: "DELETE" });
}

async function fetchAssets() {
  return (await fetch(`${API_BASE}/assets`)).json();
}

async function sendChat(message, sessionId, onEvent, signal, options) {
  // Timeout for initial connection — if the server doesn't respond in 30s, fail
  const controller = signal ? undefined : new AbortController();
  const effectiveSignal = signal || controller?.signal;
  const connectTimeout = setTimeout(() => controller?.abort(), 30000);

  let response;
  try {
    response = await fetch(`${API_BASE}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message,
        sessionId,
        skills: options?.skills,
        feedback: options?.feedback,
        skillSelectionMode: options?.skillSelectionMode,
      }),
      signal: effectiveSignal,
    });
  } finally {
    clearTimeout(connectTimeout);
  }

  if (!response.ok || !response.body) {
    throw new Error(`Server error: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastDataTime = Date.now();
  // Server sends a `: ping` heartbeat every 15s, so 2 min of total silence
  // (heartbeats AND real events both gone) means the connection is genuinely dead.
  // This must NOT be used to bound how long agent work can take — only to detect
  // a silently-dropped SSE stream (tab throttled, network drop, proxy idle-cut).
  const IDLE_TIMEOUT = 120000;

  for (;;) {
    // Race between read and idle timeout
    const readPromise = reader.read();
    const timeoutPromise = new Promise((_, reject) => {
      const check = setInterval(() => {
        if (Date.now() - lastDataTime > IDLE_TIMEOUT) {
          clearInterval(check);
          reader.cancel();
          reject(new Error("Connection idle timeout"));
        }
      }, 5000);
      readPromise.then(() => clearInterval(check)).catch(() => clearInterval(check));
    });

    let result;
    try {
      result = await Promise.race([readPromise, timeoutPromise]);
    } catch (err) {
      // Idle timeout — treat as done
      onEvent({ type: "done", data: { sessionId: null, isError: true, latestScreenshot: null } });
      break;
    }

    const { done, value } = result;
    if (done) break;

    lastDataTime = Date.now();
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";

      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }

      if (data) {
        try {
          const parsed = JSON.parse(data);
          onEvent({ type: eventType, data: parsed });
        } catch {}
      }
    }
  }
}

async function voteOnBattle(battleId, winner) {
  return (
    await fetch(`${API_BASE}/arena/battles/${battleId}/vote`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ winner }),
    })
  ).json();
}

async function fetchLeaderboard() {
  return (await fetch(`${API_BASE}/arena/leaderboard`)).json();
}

async function fetchGallery(options) {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit));
  if (options?.offset) params.set("offset", String(options.offset));
  if (options?.sort) params.set("sort", options.sort);

  const query = params.toString() ? `?${params}` : "";
  const result = await (await fetch(`${API_BASE}/arena/gallery${query}`)).json();
  return Array.isArray(result) ? result : result.items || [];
}

async function shareToGallery(item) {
  return (
    await fetch(`${API_BASE}/arena/gallery`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(item),
    })
  ).json();
}

async function fetchAgents() {
  return (await fetch(`${API_BASE}/agents`)).json();
}

async function updateAgent(id, settings) {
  return (
    await fetch(`${API_BASE}/agents/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    })
  ).json();
}

async function runArena(prompt, skills, onEvent, signal) {
  const response = await fetch(`${API_BASE}/arena/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ prompt, skills }),
    signal,
  });

  if (!response.ok || !response.body) {
    throw new Error(`Server error: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";

      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }

      if (data) {
        try {
          const parsed = JSON.parse(data);
          onEvent(eventType, parsed);
        } catch {}
      }
    }
  }
}

async function fetchTools() {
  return (await fetch(`${API_BASE}/tools`)).json();
}

async function fetchEvolutionConfig() {
  return (await fetch(`${API_BASE}/evolution/config`)).json();
}

async function updateEvolutionConfig(enabled) {
  return (
    await fetch(`${API_BASE}/evolution/config`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: Boolean(enabled), source: "scene_agent_toggle" }),
    })
  ).json();
}

async function updateToolProcedure(id, patch) {
  return (
    await fetch(`${API_BASE}/tools/${id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch),
    })
  ).json();
}

async function deleteToolProcedure(id) {
  return (
    await fetch(`${API_BASE}/tools/${id}`, {
      method: "DELETE",
    })
  ).json();
}

async function sendCameraCommand(cmd, args = []) {
  await fetch("/api/camera", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ cmd, args }),
  });
}

// ─── Utility ─────────────────────────────────────────────────────────────────

let messageCounter = 0;

function generateMessageId() {
  return `msg-${++messageCounter}-${Date.now()}`;
}

function isLearnedSkillMeta(skill) {
  if (!skill || typeof skill !== "object") return false;
  const source = String(skill.source || "").toLowerCase();
  const tags = Array.isArray(skill.tags) ? skill.tags : [];
  return source === "custom" && tags.includes("learned");
}

function headerButtonStyle(color) {
  return {
    padding: "3px 11px",
    fontSize: 12,
    background: "var(--panel)",
    border: `1px solid ${color}55`,
    borderRadius: 8,
    color,
    cursor: "pointer",
    fontWeight: 600,
    fontFamily: "inherit",
    boxShadow: "0 1px 2px rgba(15,23,42,.04)",
  };
}

// ─── ToolCallBlock ───────────────────────────────────────────────────────────

const ToolCallBlock = React.memo(function ToolCallBlock({ tool }) {
  const [expanded, setExpanded] = useState(false);

  const iconFn = TOOL_ICONS[tool.displayName] || ICONS.hammer;
  const statusColor = {
    starting: "#64748b",
    running: "#f59e0b",
    done: "#16a34a",
    error: "#dc2626",
  }[tool.status];
  const displayName = tool.displayName || tool.name.replace(/^mcp__\w+__/, "");

  let paramSummary = "";
  try {
    const input = tool.input || (tool.inputBuffer ? JSON.parse(tool.inputBuffer) : null);
    if (input) {
      paramSummary = Object.keys(input)
        .slice(0, 2)
        .map((key) => {
          const val = input[key];
          const str = Array.isArray(val) ? `[${val.join(",")}]` : String(val);
          return `${key}: ${str.slice(0, 30)}`;
        })
        .join(", ");
    }
  } catch {}

  return (
    <div
      style={{
        margin: "4px 0",
        border: "1px solid var(--line)",
        borderRadius: 8,
        overflow: "hidden",
        background: "var(--panel-2)",
      }}
    >
      <button
        onClick={() => setExpanded((prev) => !prev)}
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "5px 10px",
          background: "none",
          border: "none",
          color: "var(--ink-3)",
          cursor: "pointer",
          textAlign: "left",
          fontSize: 12,
        }}
      >
        <span
          style={{
            display: "inline-block",
            width: 7,
            height: 7,
            borderRadius: "50%",
            background: statusColor,
            flexShrink: 0,
            ...(tool.status === "running"
              ? { animation: "pulse 1s ease-in-out infinite" }
              : {}),
          }}
        />
        <span style={{ fontSize: 13, display: "inline-flex", alignItems: "center" }}>{iconFn(13)}</span>
        <span style={{ fontFamily: "monospace", color: "var(--blue)", fontWeight: 600 }}>
          {displayName}
        </span>
        {paramSummary && (
          <span
            style={{
              color: "var(--ink-2)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
              flex: 1,
              fontSize: 12,
            }}
          >
            ({paramSummary})
          </span>
        )}
        {tool.status === "running" && !tool.input && (
          <span style={{ color: "#f59e0b", fontSize: 12, marginLeft: "auto" }}>
            running…
          </span>
        )}
        <span style={{ marginLeft: "auto", fontSize: 12, flexShrink: 0 }}>
          {expanded ? "▲" : "▼"}
        </span>
      </button>

      {expanded && (
        <div style={{ padding: "8px 10px", borderTop: "1px solid var(--line)" }}>
          {(tool.input || tool.inputBuffer) && (
            <div style={{ marginBottom: 6 }}>
              <div
                style={{
                  color: "var(--ink-2)",
                  fontSize: 12,
                  marginBottom: 3,
                  textTransform: "uppercase",
                  letterSpacing: 1,
                }}
              >
                Input
              </div>
              <pre
                style={{
                  margin: 0,
                  fontSize: 12,
                  color: "var(--ink-2)",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {tool.input ? JSON.stringify(tool.input, null, 2) : tool.inputBuffer}
              </pre>
            </div>
          )}
          {tool.result && (
            <div>
              <div
                style={{
                  color: "var(--ink-3)",
                  fontSize: 12,
                  marginBottom: 3,
                  textTransform: "uppercase",
                  letterSpacing: 1,
                }}
              >
                Result
              </div>
              <pre
                style={{
                  margin: 0,
                  fontSize: 12,
                  color: tool.isError ? "#dc2626" : "#16a34a",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                  maxHeight: 200,
                  overflow: "auto",
                }}
              >
                {tool.result}
              </pre>
            </div>
          )}
          {tool.screenshot && (
            <div style={{ marginTop: 8 }}>
              <img
                src={tool.screenshot + `?t=${Date.now()}`}
                alt="UE screenshot"
                style={{
                  maxWidth: "100%",
                  borderRadius: 4,
                  border: "1px solid var(--line)",
                }}
              />
            </div>
          )}
        </div>
      )}

      <style>{`@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} } @keyframes spin { to{transform:rotate(360deg)} }`}</style>
    </div>
  );
}); // end React.memo(ToolCallBlock)

// ─── SkillItem ───────────────────────────────────────────────────────────────

function SkillItem({ skill, active, onToggle, onPreview, disabled }) {
  const toggle = () => {
    if (disabled) return;
    onToggle();
  };

  return (
    <div
      style={{
        padding: "8px 10px",
        borderRadius: 6,
        border: `1px solid ${active ? "var(--blue)" : "var(--line)"}`,
        background: active ? "var(--blue-soft)" : "#ffffff",
        transition: "all 0.15s",
        opacity: disabled ? 0.75 : 1,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <span
          onClick={toggle}
          style={{
            width: 14,
            height: 14,
            borderRadius: 3,
            border: `2px solid ${active ? "var(--blue)" : "var(--line)"}`,
            background: active ? "var(--blue)" : "transparent",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 12,
            color: "#fff",
            flexShrink: 0,
            cursor: disabled ? "not-allowed" : "pointer",
          }}
        >
          {active && "✓"}
        </span>
        <span
          onClick={toggle}
          style={{
            fontSize: 12,
            fontWeight: 500,
            color: "var(--ink)",
            cursor: disabled ? "not-allowed" : "pointer",
            flex: 1,
          }}
        >
          {skill.name}
        </span>
        <button
          onClick={(e) => {
            e.stopPropagation();
            onPreview();
          }}
          style={{
            padding: "2px 6px",
            fontSize: 12,
            background: "var(--panel-2)",
            border: "1px solid var(--line)",
            borderRadius: 4,
            color: "var(--ink-3)",
            cursor: "pointer",
          }}
          title="Preview skill details"
        >
          Preview
        </button>
        <span
          style={{
            fontSize: 12,
            padding: "1px 5px",
            borderRadius: 4,
            background: skill.source === "custom" ? "var(--blue-soft)" : "var(--panel-2)",
            color: skill.source === "custom" ? "var(--blue)" : "var(--ink-3)",
          }}
        >
          {skill.source}
        </span>
      </div>

      <div
        style={{
          fontSize: 12,
          color: "var(--ink-3)",
          marginTop: 4,
          marginLeft: 20,
          lineHeight: 1.4,
        }}
      >
        {skill.description}
      </div>

      {skill.tags.length > 0 && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: 4,
            marginTop: 5,
            marginLeft: 20,
          }}
        >
          {skill.tags.map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: 12,
                padding: "1px 5px",
                borderRadius: 4,
                background: (TAG_COLORS[tag] || "var(--line)") + "33",
                color: TAG_COLORS[tag] || "#64748b",
                border: `1px solid ${TAG_COLORS[tag] || "var(--line)"}44`,
              }}
            >
              {tag}
            </span>
          ))}
        </div>
      )}

      {skill.dependencies.length > 0 && (
        <div
          style={{
            fontSize: 12,
            color: "var(--ink-2)",
            marginTop: 4,
            marginLeft: 20,
          }}
        >
          Depends on: {skill.dependencies.join(", ")}
        </div>
      )}
    </div>
  );
}

// ─── SkillPreviewModal ───────────────────────────────────────────────────────

function SkillPreviewModal({ skill, onClose, onDelete }) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.7)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 700,
          maxHeight: "80vh",
          background: "var(--bg)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "14px 18px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>
              {skill.name}
            </div>
            <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 3 }}>
              v{skill.version} by {skill.author}
              <span
                style={{
                  marginLeft: 8,
                  padding: "1px 5px",
                  borderRadius: 4,
                  background: skill.source === "custom" ? "var(--blue-soft)" : "#e6e9ef",
                  color: skill.source === "custom" ? "var(--blue)" : "var(--ink-3)",
                  fontSize: 12,
                }}
              >
                {skill.source}
              </span>
            </div>
          </div>
          {onDelete && (
            <button
              onClick={onDelete}
              style={{
                padding: "4px 10px",
                fontSize: 12,
                background: "rgba(255,95,99,0.1)",
                border: "1px solid rgba(255,95,99,0.3)",
                borderRadius: 6,
                color: "var(--red)",
                cursor: "pointer",
              }}
            >
              Delete
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              padding: "4px 10px",
              fontSize: 14,
              background: "transparent",
              border: "none",
              color: "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        {/* Description + Tags */}
        <div style={{ padding: "10px 18px", borderBottom: "1px solid var(--line)" }}>
          <div style={{ fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>
            {skill.description}
          </div>
          {skill.tags.length > 0 && (
            <div
              style={{
                display: "flex",
                flexWrap: "wrap",
                gap: 4,
                marginTop: 8,
              }}
            >
              {skill.tags.map((tag) => (
                <span
                  key={tag}
                  style={{
                    fontSize: 12,
                    padding: "2px 7px",
                    borderRadius: 4,
                    background: (TAG_COLORS[tag] || "var(--line)") + "33",
                    color: TAG_COLORS[tag] || "#64748b",
                    border: `1px solid ${TAG_COLORS[tag] || "var(--line)"}44`,
                  }}
                >
                  {tag}
                </span>
              ))}
            </div>
          )}
        </div>

        {/* Content */}
        <div style={{ flex: 1, overflow: "auto", padding: "14px 18px" }}>
          <pre
            style={{
              fontSize: 12,
              color: "var(--ink)",
              lineHeight: 1.6,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontFamily:
                "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
              margin: 0,
            }}
          >
            {skill.content}
          </pre>
        </div>
      </div>
    </div>
  );
}

// ─── CreateSkillModal ────────────────────────────────────────────────────────

function CreateSkillModal({ onClose, onCreated }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [content, setContent] = useState(`# My Custom Skill

## Overview
Describe what this skill does.

## Instructions
Provide detailed instructions for the AI agent.
`);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    if (!id.trim() || !name.trim() || !content.trim()) {
      setError("ID, name, and content are required");
      return;
    }
    if (!/^[a-z0-9_]+$/.test(id)) {
      setError("ID must be lowercase letters, numbers, and underscores only");
      return;
    }
    setSaving(true);
    try {
      await createSkill({
        id: id.trim(),
        name: name.trim(),
        description: description.trim(),
        tags: tags
          .split(",")
          .map((t) => t.trim())
          .filter(Boolean),
        content: content.trim(),
      });
      onCreated();
    } catch {
      setError("Failed to save skill");
    } finally {
      setSaving(false);
    }
  };

  const inputStyle = {
    width: "100%",
    padding: "6px 10px",
    fontSize: 12,
    background: "var(--panel)",
    border: "1px solid var(--line)",
    borderRadius: 6,
    color: "var(--ink)",
    outline: "none",
    boxSizing: "border-box",
  };

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.7)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 650,
          maxHeight: "85vh",
          background: "var(--bg)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "14px 18px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>
            Create Custom Skill
          </span>
          <button
            onClick={onClose}
            style={{
              marginLeft: "auto",
              padding: "4px 10px",
              fontSize: 14,
              background: "transparent",
              border: "none",
              color: "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        {/* Form */}
        <div
          style={{
            flex: 1,
            overflow: "auto",
            padding: "14px 18px",
            display: "flex",
            flexDirection: "column",
            gap: 12,
          }}
        >
          <div>
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
              Skill ID (lowercase, no spaces)
            </label>
            <input
              value={id}
              onChange={(e) => setId(e.target.value)}
              placeholder="my_custom_skill"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
              Name
            </label>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="My Custom Skill"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
              Description (short summary)
            </label>
            <input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What this skill teaches the agent to do"
              style={inputStyle}
            />
          </div>
          <div>
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
              Tags (comma-separated)
            </label>
            <input
              value={tags}
              onChange={(e) => setTags(e.target.value)}
              placeholder="buildings, layout, custom"
              style={inputStyle}
            />
          </div>
          <div style={{ flex: 1 }}>
            <label style={{ fontSize: 12, color: "var(--ink-3)", display: "block", marginBottom: 4 }}>
              Content (Markdown — instructions for the AI agent)
            </label>
            <textarea
              value={content}
              onChange={(e) => setContent(e.target.value)}
              style={{
                ...inputStyle,
                height: 250,
                resize: "vertical",
                fontFamily:
                  "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
                lineHeight: 1.5,
              }}
            />
          </div>
          {error && (
            <div style={{ fontSize: 12, color: "var(--red)", padding: "4px 0" }}>{error}</div>
          )}
        </div>

        {/* Footer */}
        <div
          style={{
            padding: "12px 18px",
            borderTop: "1px solid var(--line)",
            display: "flex",
            gap: 8,
            justifyContent: "flex-end",
          }}
        >
          <button
            onClick={onClose}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "var(--panel-2)",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink)",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleSave}
            disabled={saving}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "var(--green)",
              border: "1px solid var(--green)",
              borderRadius: 6,
              color: "#fff",
              cursor: saving ? "wait" : "pointer",
              opacity: saving ? 0.7 : 1,
            }}
          >
            {saving ? "Saving..." : "Create Skill"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── SkillsPanel (sidebar in chat) ───────────────────────────────────────────

function SkillsPanel({
  selected,
  onChange,
  autoEnabled,
  onAutoEnabledChange,
  autoSelected,
}) {
  const [skills, setSkills] = useState([]);
  const [expanded, setExpanded] = useState(false);
  const [previewSkill, setPreviewSkill] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const activeSkills = autoEnabled ? autoSelected : selected;

  const reload = () => {
    fetchSkills()
      .then(setSkills)
      .catch(() => {});
  };

  // Delay initial load so it doesn't compete with critical requests at startup
  useEffect(() => { const t = setTimeout(reload, 5000); return () => clearTimeout(t); }, []);

  useEffect(() => {
    if (!expanded) return;
    reload();
    const timer = setInterval(reload, 30000);
    return () => clearInterval(timer);
  }, [expanded]);

  const toggleSkill = (id) => {
    if (autoEnabled) return;
    onChange(selected.includes(id) ? selected.filter((s) => s !== id) : [...selected, id]);
  };

  const handlePreview = async (id) => {
    try {
      const detail = await fetchSkillDetails(id);
      setPreviewSkill(detail);
    } catch {}
  };

  const handleDelete = async (id) => {
    await deleteSkill(id);
    setPreviewSkill(null);
    reload();
  };

  if (skills.length === 0) return null;

  const builtinSkills = skills.filter((s) => s.source === "builtin");
  const customSkills = skills.filter((s) => s.source === "custom");

  return (
    <div
      style={{
        padding: "6px 12px",
        borderBottom: "1px solid var(--line)",
        background: "var(--bg)",
      }}
    >
      {/* Toggle header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          cursor: "pointer",
          userSelect: "none",
        }}
        onClick={() => setExpanded(!expanded)}
      >
        <span style={{ fontSize: 12, color: "var(--ink-3)", fontFamily: "monospace" }}>
          {expanded ? "▼" : "▶"}
        </span>
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>Skills</span>
        {activeSkills.length > 0 && (
          <span
            style={{
              fontSize: 12,
              background: "var(--blue)",
              color: "#fff",
              borderRadius: 8,
              padding: "1px 6px",
              marginLeft: 4,
            }}
          >
            {activeSkills.length}
          </span>
        )}
        <div
          onClick={(e) => e.stopPropagation()}
          style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 6 }}
        >
          <span style={{ fontSize: 12, color: "var(--ink-3)" }}>Auto-select skills</span>
          <button
            onClick={() => onAutoEnabledChange(!autoEnabled)}
            style={{
              width: 34,
              height: 18,
              borderRadius: 999,
              border: "1px solid var(--line)",
              background: autoEnabled ? "var(--blue)" : "var(--line)",
              padding: 1,
              position: "relative",
              cursor: "pointer",
            }}
            title={`Auto-select skills: ${autoEnabled ? "on" : "off"}`}
            aria-label="Toggle auto-select skills"
            aria-pressed={autoEnabled}
          >
            <span
              style={{
                display: "block",
                width: 14,
                height: 14,
                borderRadius: "50%",
                background: "#fff",
                transform: autoEnabled ? "translateX(16px)" : "translateX(0)",
                transition: "transform 0.15s ease",
              }}
            />
          </button>
        </div>
        <span style={{ fontSize: 12, color: "var(--ink-2)", marginLeft: 6 }}>
          {skills.length} available
        </span>
      </div>

      {/* Expanded skill list */}
      {expanded && (
        <div
          style={{
            marginTop: 6,
            display: "flex",
            flexDirection: "column",
            gap: 4,
            maxHeight: 260,
            overflowY: "auto",
            paddingRight: 4,
          }}
        >
          <div
            style={{
              fontSize: 12,
              color: "var(--ink-3)",
              border: "1px solid var(--line)",
              background: "var(--bg)",
              borderRadius: 6,
              padding: "6px 8px",
              marginBottom: 2,
            }}
          >
            {autoEnabled
              ? "Auto mode: the AI pre-selects relevant skills before each run."
              : "Manual mode: check the exact skills you want active."}
          </div>
          {builtinSkills.length > 0 && (
            <div
              style={{
                fontSize: 12,
                color: "var(--ink-2)",
                fontWeight: 600,
                padding: "4px 0 2px",
              }}
            >
              BUILTIN
            </div>
          )}
          {builtinSkills.map((s) => (
            <SkillItem
              key={s.id}
              skill={s}
              active={activeSkills.includes(s.id)}
              onToggle={() => toggleSkill(s.id)}
              onPreview={() => handlePreview(s.id)}
              disabled={autoEnabled}
            />
          ))}

          {customSkills.length > 0 && (
            <div
              style={{
                fontSize: 12,
                color: "var(--ink-2)",
                fontWeight: 600,
                padding: "6px 0 2px",
              }}
            >
              CUSTOM
            </div>
          )}
          {customSkills.map((s) => (
            <SkillItem
              key={s.id}
              skill={s}
              active={activeSkills.includes(s.id)}
              onToggle={() => toggleSkill(s.id)}
              onPreview={() => handlePreview(s.id)}
              disabled={autoEnabled}
            />
          ))}

          <button
            onClick={(e) => {
              e.stopPropagation();
              setShowCreate(true);
            }}
            style={{
              padding: "6px 10px",
              marginTop: 4,
              borderRadius: 6,
              border: "1px dashed var(--line)",
              background: "transparent",
              color: "var(--blue)",
              fontSize: 12,
              cursor: "pointer",
              display: "flex",
              alignItems: "center",
              gap: 6,
              justifyContent: "center",
            }}
          >
            + Add Custom Skill
          </button>
        </div>
      )}

      {/* Modals */}
      {previewSkill && (
        <SkillPreviewModal
          skill={previewSkill}
          onClose={() => setPreviewSkill(null)}
          onDelete={previewSkill.source === "custom" ? () => handleDelete(previewSkill.id) : undefined}
        />
      )}
      {showCreate && (
        <CreateSkillModal
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            reload();
          }}
        />
      )}
    </div>
  );
}

// ─── AnnotateOverlay ─────────────────────────────────────────────────────────

function AnnotateOverlay({ src, onSubmitFeedback, onCancel }) {
  const [points, setPoints] = useState([]);
  const [feedbackText, setFeedbackText] = useState("");
  const imgRef = useRef(null);

  const handleImageClick = useCallback((e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    const text = prompt("Describe what to change at this point:");
    if (text) setPoints((prev) => [...prev, { x, y, text }]);
  }, []);

  const removePoint = (index) => {
    setPoints((prev) => prev.filter((_, i) => i !== index));
  };

  const handleSubmit = () => {
    let result = feedbackText;
    if (points.length > 0) {
      result += "\n\nAnnotated points on the screenshot:";
      for (const pt of points) {
        const pctX = Math.round(pt.x * 100);
        const pctY = Math.round(pt.y * 100);
        result += `\n- At position (${pctX}% from left, ${pctY}% from top): "${pt.text}"`;
      }
    }
    onSubmitFeedback(result.trim(), points);
  };

  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        background: "rgba(0,0,0,0.85)",
        display: "flex",
        flexDirection: "column",
        zIndex: 100,
      }}
    >
      {/* Toolbar */}
      <div
        style={{
          padding: "8px 14px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 8,
          background: "var(--panel)",
        }}
      >
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--orange)" }}>
          Annotate Screenshot
        </span>
        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
          Click on the image to add feedback points
        </span>
        <div style={{ marginLeft: "auto", display: "flex", gap: 6 }}>
          <button
            onClick={onCancel}
            style={{
              padding: "4px 12px",
              fontSize: 12,
              borderRadius: 4,
              border: "1px solid var(--line)",
              background: "var(--panel-2)",
              color: "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            onClick={handleSubmit}
            disabled={!feedbackText && points.length === 0}
            style={{
              padding: "4px 12px",
              fontSize: 12,
              borderRadius: 4,
              border: "1px solid var(--blue)",
              background: "var(--blue)",
              color: "#fff",
              cursor: "pointer",
              opacity: !feedbackText && points.length === 0 ? 0.5 : 1,
            }}
          >
            Send Feedback
          </button>
        </div>
      </div>

      {/* Body */}
      <div style={{ flex: 1, display: "flex", gap: 0, overflow: "hidden" }}>
        {/* Image area */}
        <div style={{ flex: 1, position: "relative", overflow: "hidden" }}>
          <div
            onClick={handleImageClick}
            style={{
              width: "100%",
              height: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              cursor: "crosshair",
              position: "relative",
            }}
          >
            <img
              ref={imgRef}
              src={src}
              alt="Annotate"
              style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain" }}
            />
            {points.map((pt, i) => (
              <div
                key={i}
                style={{
                  position: "absolute",
                  left: `${pt.x * 100}%`,
                  top: `${pt.y * 100}%`,
                  transform: "translate(-50%, -50%)",
                  pointerEvents: "auto",
                }}
              >
                <div
                  style={{
                    width: 24,
                    height: 24,
                    borderRadius: "50%",
                    background: "var(--orange)",
                    border: "2px solid #fff",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                    fontSize: 12,
                    fontWeight: 700,
                    color: "#fff",
                    cursor: "pointer",
                  }}
                  onClick={(e) => {
                    e.stopPropagation();
                    removePoint(i);
                  }}
                >
                  {i + 1}
                </div>
                <div
                  style={{
                    position: "absolute",
                    left: 16,
                    top: -4,
                    background: "var(--panel-2)",
                    border: "1px solid var(--line)",
                    borderRadius: 4,
                    padding: "2px 6px",
                    fontSize: 12,
                    color: "var(--ink)",
                    whiteSpace: "nowrap",
                    maxWidth: 200,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {pt.text}
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Sidebar */}
        <div
          style={{
            width: 260,
            borderLeft: "1px solid #e6e9ef",
            background: "var(--bg)",
            display: "flex",
            flexDirection: "column",
            padding: 12,
            gap: 8,
          }}
        >
          <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>Feedback</span>
          <textarea
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
            placeholder="Describe what to change overall..."
            style={{
              flex: 1,
              resize: "none",
              background: "var(--panel)",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink)",
              padding: 8,
              fontSize: 12,
              fontFamily: "inherit",
            }}
          />
          {points.length > 0 && (
            <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
              <div style={{ fontWeight: 600, marginBottom: 4 }}>Annotations:</div>
              {points.map((pt, i) => (
                <div
                  key={i}
                  style={{
                    display: "flex",
                    gap: 4,
                    alignItems: "flex-start",
                    marginBottom: 4,
                  }}
                >
                  <span
                    style={{
                      width: 16,
                      height: 16,
                      borderRadius: "50%",
                      background: "var(--orange)",
                      fontSize: 12,
                      color: "#fff",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      flexShrink: 0,
                    }}
                  >
                    {i + 1}
                  </span>
                  <span style={{ fontSize: 12, color: "var(--ink)" }}>{pt.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── ChatMessage ─────────────────────────────────────────────────────────────

const ChatMessage = React.memo(function ChatMessage({ message }) {
  const isUser = message.role === "user";

  const bubbleContent = isUser ? (
    <div style={{ color:"var(--ink)", fontSize:13, whiteSpace:"pre-wrap" }}>{message.content}</div>
  ) : (
    <>
      {message.waiting && (
        <div style={{ color:"#64748b", fontSize:12, display:"flex", alignItems:"center", gap:8, padding:"2px 0" }}>
          <span style={{ display:"inline-block", width:8, height:8, borderRadius:"50%", border:"2px solid var(--blue)", borderTopColor:"transparent", animation:"spin 1s linear infinite" }} />
          Waiting for AI model...
        </div>
      )}
      {message.blocks
        ? message.blocks.map((block, idx) =>
            block.type === "text" ? (
              block.content ? (
                <div className="markdown" key={"t"+idx} style={{ color:"#0f172a", fontSize:13 }}>
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{block.content}</ReactMarkdown>
                </div>
              ) : null
            ) : (
              <ToolCallBlock key={block.toolId} tool={(message.toolCalls||[]).find(tc=>tc.id===block.toolId)} />
            )
          )
        : [
            message.content && (
              <div className="markdown" key="content" style={{ color:"var(--ink,#0f172a)", fontSize:13 }}>
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
              </div>
            ),
            (message.toolCalls||[]).map(tc=><ToolCallBlock key={tc.id} tool={tc}/>),
          ]}
    </>
  );

  const bubble = (
    <div className={isUser ? "sw-bubble-user" : "sw-bubble-assistant"} style={{
      padding:"9px 12px",
      borderRadius: isUser ? "12px 12px 4px 12px" : "12px 12px 12px 4px",
      background: isUser ? "var(--user-bubble,#eff4ff)" : "var(--assistant-bubble,#ffffff)",
      border:`1px solid ${isUser?"var(--blue-soft,#dbe6ff)":"var(--line,#e6e9ef)"}`,
      boxShadow:"0 1px 2px rgba(15,23,42,.04)",
    }}>
      {bubbleContent}
    </div>
  );

  if (isUser) {
    return (
      <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-end", gap:3, marginBottom:2 }}>
        <div style={{ display:"flex", alignItems:"flex-end", gap:7, maxWidth:"86%" }}>
          <div style={{ flex:1, minWidth:0 }}>{bubble}</div>
          <div style={{
            width:26, height:26, borderRadius:"50%", flexShrink:0,
            background:"linear-gradient(135deg,#e0e7ff,#c7d2fe)",
            display:"flex", alignItems:"center", justifyContent:"center",
          }}>
            <svg viewBox="0 0 24 24" fill="none" width="14" height="14">
              <circle cx="12" cy="8" r="4" fill="#6366f1"/>
              <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" fill="#6366f1"/>
            </svg>
          </div>
        </div>
        <div style={{ fontSize:12, color:"#64748b", paddingRight:33 }}>
          You · {new Date(message.timestamp).toLocaleTimeString()}
        </div>
      </div>
    );
  }

  return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"flex-start", gap:3, marginBottom:2 }}>
      <div style={{ display:"flex", alignItems:"flex-end", gap:7, maxWidth:"86%" }}>
        <div style={{
          width:26, height:26, borderRadius:"50%", flexShrink:0, overflow:"hidden",
          background:"linear-gradient(140deg,#fef3e7,#f5e3cf)",
          border:"1px solid #f3dfc4",
          boxShadow:"0 1px 4px rgba(234,88,12,.15)",
        }}>
          <img src="/SimCoder.png" alt="SimCoder" style={{ width:"100%", height:"100%", objectFit:"contain", padding:2, display:"block" }}/>
        </div>
        <div style={{ flex:1, minWidth:0 }}>{bubble}</div>
      </div>
      <div style={{ fontSize:12, color:"#64748b", paddingLeft:33 }}>
        SimCoder · {new Date(message.timestamp).toLocaleTimeString()}
      </div>
    </div>
  );
}); // end React.memo(ChatMessage)

// ─── TypingIndicator ─────────────────────────────────────────────────────────

function TypingIndicator() {
  return (
    <div style={{ display: "flex", gap: 4, alignItems: "center", padding: "4px 0" }}>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: "#64748b",
            animation: `bounce 1.2s ease-in-out ${i * 0.2}s infinite`,
          }}
        />
      ))}
      <style>
        {
          "@keyframes bounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-6px)} }"
        }
      </style>
    </div>
  );
}

// ─── ChatPanel ───────────────────────────────────────────────────────────────

function ChatPanel({ onScreenshotUpdate, onRef, onSessionChange, onChatDone }) {
  const [messages, setMessages] = useState(() => [buildWelcomeMessage()]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [sessionId, setSessionId] = useState(null);
  const [mcpStatus, setMcpStatus] = useState("–");
  const [selectedSkills, setSelectedSkills] = useState([]);
  const [autoSkillSelectionEnabled, setAutoSkillSelectionEnabled] = useState(true);
  const [autoSelectedSkills, setAutoSelectedSkills] = useState([]);
  const [autoSelectingSkills, setAutoSelectingSkills] = useState(false);
  const [autoSelectionError, setAutoSelectionError] = useState("");
  const [selfEvolutionEnabled, setSelfEvolutionEnabled] = useState(null);
  const [latestScreenshot, setLatestScreenshot] = useState(null);
  const [annotating, setAnnotating] = useState(false);
  const [turnCount, setTurnCount] = useState(0);
  const scrollRef = useRef(null);
  const abortRef = useRef(null);
  const textareaRef = useRef(null);
  const selfEvolutionReqSeqRef = useRef(0);
  const activeSkills = autoSkillSelectionEnabled ? autoSelectedSkills : selectedSkills;
  const selfEvolutionReady = typeof selfEvolutionEnabled === "boolean";
  const selfEvolutionOn = selfEvolutionEnabled === true;

  // Expose methods to parent
  useEffect(() => {
    onRef?.({
      insertText: (text) =>
        setInput((prev) => (prev ? prev + "\n" + text : text)),
      loadScene: (scene) => {
        if (scene.sessionId) {
          setSessionId(scene.sessionId);
          setMessages(
            scene.chatHistory || [
              {
                id: generateMessageId(),
                role: "assistant",
                content: `Loaded scene: **${scene.name}**\nContinuing from previous session.`,
                timestamp: Date.now(),
              },
            ]
          );
        }
      },
    });
  }, [onRef]);

  useEffect(() => {
    let mounted = true;
    fetchEvolutionConfig()
      .then((cfg) => {
        if (!mounted) return;
        if (cfg && typeof cfg.enabled === "boolean") {
          setSelfEvolutionEnabled(cfg.enabled);
          return;
        }
        setSelfEvolutionEnabled(true);
      })
      .catch(() => {
        if (!mounted) return;
        setSelfEvolutionEnabled(true);
      });
    return () => {
      mounted = false;
    };
  }, []);

  // Auto-scroll
  useEffect(() => {
    scrollRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  // Auto-resize textarea
  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.style.height = "auto";
      textareaRef.current.style.height =
        Math.min(textareaRef.current.scrollHeight, 160) + "px";
    }
  }, [input]);

  const handleSend = useCallback(
    async (overrideMessage, feedbackText) => {
      const text = (overrideMessage || input).trim();
      if (!text || loading) return;
      if (!overrideMessage) setInput("");
      if (autoSkillSelectionEnabled) {
        setAutoSelectionError("");
      }

      const userMsg = {
        id: generateMessageId(),
        role: "user",
        content: text,
        timestamp: Date.now(),
      };
      const assistantId = generateMessageId();
      const assistantMsg = {
        id: assistantId,
        role: "assistant",
        content: "",
        waiting: true,  // Show "Waiting for AI model..." until first event
        toolCalls: [],
        timestamp: Date.now(),
      };

      setMessages((prev) => [...prev, userMsg, assistantMsg]);
      setLoading(true);
      setTurnCount((c) => c + 1);

      const controller = new AbortController();
      abortRef.current = controller;
      const inputBuffers = new Map();

      // P2-2 SSE text batching: buffer rapid text deltas, flush via rAF to avoid
      // a React setState per character during fast streaming.
      let _textBuf = "";
      let _rafPending = false;
      const _flushTextBuf = (msgId) => {
        if (!_textBuf) return;
        const delta = _textBuf;
        _textBuf = "";
        _rafPending = false;
        setMessages((prev) => {
          const updated = [...prev];
          const idx = updated.findIndex((m) => m.id === msgId);
          if (idx === -1) return prev;
          const msg = { ...updated[idx] };
          const blocks = msg.blocks || [];
          const last = blocks[blocks.length - 1];
          if (last?.type === "text") {
            msg.blocks = [...blocks.slice(0, -1), { ...last, content: last.content + delta }];
          } else {
            msg.blocks = [...blocks, { type: "text", content: delta }];
          }
          msg.content = (msg.content || "") + delta;
          updated[idx] = msg;
          return updated;
        });
      };

      // NOTE: no absolute time limit on agent runs. Long scenes can legitimately
      // take 10+ minutes. Dead-connection detection lives inside sendChat's
      // reader-level idle timer (refreshed by server `: ping` heartbeats).
      // To stop a runaway agent, use the Stop button (handleStop → controller.abort).

      try {
        await sendChat(
          text,
          sessionId,
          (event) => {
            setMessages((prev) => {
              const updated = [...prev];
              const idx = updated.findIndex((m) => m.id === assistantId);
              if (idx === -1) return prev;
              const msg = { ...updated[idx] };
              // Clear waiting flag on first real event
              if (msg.waiting && (event.type === "text" || event.type === "tool_start" || event.type === "system")) {
                msg.waiting = false;
              }

              switch (event.type) {
                case "skill_selection_start": {
                  setAutoSelectionError("");
                  setAutoSelectingSkills(true);
                  break;
                }
                case "skill_selection_error": {
                  setAutoSelectingSkills(false);
                  setAutoSelectionError(event.data?.message || "Auto skill selection failed");
                  break;
                }
                case "skill_selection_done": {
                  setAutoSelectingSkills(false);
                  const mode = event.data?.mode;
                  const selected = Array.isArray(event.data?.selectedSkills)
                    ? event.data.selectedSkills
                    : [];
                  if (mode === "auto") {
                    setAutoSelectionError("");
                    setAutoSelectedSkills(selected);
                    setSelectedSkills(selected);
                  } else if (mode === "manual") {
                    setAutoSelectedSkills([]);
                  }
                  break;
                }
                case "system": {
                  const connected = event.data.mcpServers
                    .filter((s) => s.status === "connected")
                    .map((s) => s.name);
                  setMcpStatus(connected.length ? `✓ ${connected.join(", ")}` : "✗ none");
                  console.log("[CTX-DEBUG] system event, sessionId:", event.data.sessionId);
                  if (event.data.sessionId) {
                    setSessionId(event.data.sessionId);
                    onSessionChange?.(event.data.sessionId);
                  }
                  break;
                }
                case "text": {
                  // P2-2: buffer into _textBuf, flush via rAF — avoids setState per character
                  _textBuf += event.data.delta || "";
                  if (!_rafPending) {
                    _rafPending = true;
                    requestAnimationFrame(() => _flushTextBuf(assistantId));
                  }
                  // Don't update msg here — the rAF flush handles it separately
                  return prev; // bail out of setMessages for text events
                }
                case "tool_start": {
                  const toolCall = {
                    id: event.data.id,
                    name: event.data.name,
                    displayName: event.data.displayName,
                    status: "running",
                    inputBuffer: "",
                  };
                  inputBuffers.set(toolCall.id, "");
                  msg.toolCalls = [...(msg.toolCalls || []), toolCall];
                  msg.blocks = [
                    ...(msg.blocks || []),
                    { type: "tool", toolId: toolCall.id },
                  ];
                  break;
                }
                case "tool_input": {
                  const calls = msg.toolCalls || [];
                  const lastCall = calls[calls.length - 1];
                  if (lastCall) {
                    const buf = (inputBuffers.get(lastCall.id) || "") + event.data.delta;
                    inputBuffers.set(lastCall.id, buf);
                    msg.toolCalls = (msg.toolCalls || []).map((tc) =>
                      tc.id === lastCall.id ? { ...tc, inputBuffer: buf } : tc
                    );
                  }
                  break;
                }
                case "tool_details": {
                  const toolId = event.data.id;
                  msg.toolCalls = (msg.toolCalls || []).map((tc) =>
                    tc.id === toolId
                      ? { ...tc, input: event.data.input, displayName: event.data.displayName }
                      : tc
                  );
                  break;
                }
                case "tool_result": {
                  const toolUseId = event.data.toolUseId;
                  const isError = event.data.isError;
                  msg.toolCalls = (msg.toolCalls || []).map((tc) =>
                    tc.id === toolUseId
                      ? {
                          ...tc,
                          result: event.data.result,
                          isError,
                          status: isError ? "error" : "done",
                        }
                      : tc
                  );
                  break;
                }
                case "screenshot": {
                  const filepath = event.data.filepath;
                  onScreenshotUpdate(filepath);
                  setLatestScreenshot(filepath);
                  const toolUseId = event.data.toolUseId;
                  if (toolUseId) {
                    msg.toolCalls = (msg.toolCalls || []).map((tc) =>
                      tc.id === toolUseId ? { ...tc, screenshot: filepath } : tc
                    );
                  }
                  break;
                }
                case "done": {
                  const sid = event.data.sessionId;
                  const isErr = event.data.isError;
                  // Always keep sessionId — it's the stable studio session, not a provider-specific transient one
                  if (sid) {
                    setSessionId(sid);
                    onSessionChange?.(sid);
                  }
                  const screenshot = event.data.latestScreenshot;
                  if (screenshot) {
                    onScreenshotUpdate(screenshot);
                    setLatestScreenshot(screenshot);
                  }
                  // If no content was streamed at all, show fallback but keep session
                  if (!msg.content && (!msg.toolCalls || msg.toolCalls.length === 0)) {
                    msg.content = isErr
                      ? "**Warning:** Agent exited unexpectedly. Try again — each message starts a fresh process."
                      : "**Warning:** No response received. Try sending your message again.";
                  }
                  onChatDone?.();
                  break;
                }
              }

              updated[idx] = msg;
              return updated;
            });
          },
          controller.signal,
          {
            skills: selectedSkills.length > 0 ? selectedSkills : undefined,
            feedback: feedbackText,
            skillSelectionMode: autoSkillSelectionEnabled ? "auto" : "manual",
          }
        );
      } catch (err) {
        if (err instanceof Error && err.name !== "AbortError") {
          setMessages((prev) => {
            const updated = [...prev];
            const idx = updated.findIndex((m) => m.id === assistantId);
            if (idx !== -1) {
              updated[idx] = {
                ...updated[idx],
                content: updated[idx].content || `Error: ${err.message}`,
              };
            }
            return updated;
          });
        }
      } finally {
        setLoading(false);
        setAutoSelectingSkills(false);
        abortRef.current = null;
      }
    },
    [input, loading, sessionId, selectedSkills, autoSkillSelectionEnabled, onScreenshotUpdate]
  );

  const handleStop = () => {
    // Tell the server to actually stop the active model run. Without this,
    // aborting the SSE alone just leaves the agent running in background
    // (server-side e.on("close") no longer kills on disconnect).
    fetch(`${API_BASE}/chat-stop`, { method: "POST" }).catch(() => {});
    abortRef.current?.abort();
    setLoading(false);
  };

  const handleKeyDown = (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleReset = () => {
    fetch(`${API_BASE}/chat-stop`, { method: "POST" }).catch(() => {});
    abortRef.current?.abort();
    setInput("");
    setLoading(false);
    setSessionId(null);
    setMcpStatus("–");
    setSelectedSkills([]);
    setAutoSelectedSkills([]);
    setAutoSelectingSkills(false);
    setAutoSelectionError("");
    setTurnCount(0);
    setLatestScreenshot(null);
    abortRef.current = null;
    setMessages([
      {
        id: generateMessageId(),
        role: "assistant",
        content: "Session reset. Starting a fresh conversation.",
        timestamp: Date.now(),
      },
    ]);
  };

  const handleSelfEvolutionToggle = async () => {
    if (!selfEvolutionReady) return;
    const prevEnabled = selfEvolutionEnabled;
    const nextEnabled = !prevEnabled;
    const reqSeq = ++selfEvolutionReqSeqRef.current;
    setSelfEvolutionEnabled(nextEnabled);
    try {
      const out = await updateEvolutionConfig(nextEnabled);
      if (reqSeq !== selfEvolutionReqSeqRef.current) return;
      if (out && typeof out.enabled === "boolean" && out.enabled !== nextEnabled) {
        setSelfEvolutionEnabled(out.enabled);
      }
    } catch {
      if (reqSeq !== selfEvolutionReqSeqRef.current) return;
      setSelfEvolutionEnabled(prevEnabled);
    }
  };

  const handleSave = async () => {
    const firstUser = messages.find((m) => m.role === "user");
    const prompt = firstUser?.content || "";
    const name = window.prompt("Scene name:", prompt.slice(0, 50) || "My Scene");
    if (name) {
      await saveScene({
        name,
        prompt,
        description: `${turnCount} turns, ${messages.length} messages`,
        sessionId: sessionId || undefined,
        skills: activeSkills,
        chatHistory: messages,
      });
      alert("Scene saved!");
    }
  };

  const handleShare = async () => {
    const firstUser = messages.find((m) => m.role === "user");
    const prompt = firstUser?.content || "Untitled scene";
    try {
      await shareToGallery({
        prompt,
        agentName: "claude-code",
        screenshots: latestScreenshot ? [latestScreenshot] : [],
        tags: activeSkills.length > 0 ? activeSkills : ["user-generated"],
        skills: activeSkills,
      });
      alert("Shared to Gallery!");
    } catch {
      alert("Failed to share to gallery");
    }
  };

  const handleAnnotationSubmit = (feedbackMsg, _points) => {
    setAnnotating(false);
    if (feedbackMsg) handleSend(feedbackMsg, feedbackMsg);
  };

  const lastMessage = messages[messages.length - 1];

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "var(--bg)",
        position: "relative",
      }}
    >
      {/* Annotation overlay */}
      {annotating && latestScreenshot && (
        <AnnotateOverlay
          src={latestScreenshot}
          onSubmitFeedback={handleAnnotationSubmit}
          onCancel={() => setAnnotating(false)}
        />
      )}

      {/* Header */}
      <div
        style={{
          padding: "10px 14px",
          borderBottom: "1px solid var(--line)",
          background: "var(--panel)",
          display: "flex",
          alignItems: "center",
          gap: 10,
          flexShrink: 0,
          boxShadow: "0 1px 2px rgba(15,23,42,.04)",
        }}
      >
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 700, fontSize: 13, color: "var(--ink)" }}>Scene Agent</div>
          <div
            style={{
              fontSize: 12,
              color: "var(--ink-3)",
              display: "flex",
              gap: 8,
              alignItems: "center",
            }}
          >
            <span>LLM provider</span>
            <span>·</span>
            <span style={{ color: mcpStatus.startsWith("✓") ? "#16a34a" : "#dc2626" }}>
              MCP: {mcpStatus}
            </span>
            <span>·</span>
            <span style={{ color: selfEvolutionOn ? "var(--orange)" : "var(--ink-3)" }}>
              Self-evolution: {selfEvolutionReady ? (selfEvolutionOn ? "on" : "off") : "syncing"}
            </span>
            {sessionId && (
              <>
                <span>·</span>
                <span style={{ color: "var(--ink-3)" }}>session: {sessionId.slice(0, 8)}</span>
                <span>·</span>
                <span style={{ color: "var(--ink-2)" }}>turn {turnCount}</span>
              </>
            )}
          </div>
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          <button
            onClick={handleSelfEvolutionToggle}
            title="Enable or disable self-evolution ingestion"
            style={{
              height: 24,
              padding: "0 10px",
              borderRadius: 7,
              border: selfEvolutionOn ? "1px solid rgba(255,157,66,0.4)" : "1px solid var(--line)",
              background: selfEvolutionOn ? "#fff7ed" : "#f8fafc",
              color: selfEvolutionOn ? "var(--orange)" : "var(--ink-3)",
              cursor: selfEvolutionReady ? "pointer" : "not-allowed",
              display: "flex",
              alignItems: "center",
              gap: 7,
              fontSize: 12,
              fontWeight: selfEvolutionOn ? 600 : 500,
              opacity: selfEvolutionReady ? 1 : 0.7,
              boxShadow: selfEvolutionOn ? "0 0 12px rgba(240,136,62,0.35)" : "none",
              transition: "all 0.18s ease",
            }}
          >
            <span
              style={{
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: selfEvolutionOn ? "var(--orange)" : "var(--line)",
                boxShadow: selfEvolutionOn ? "0 0 8px rgba(255,157,66,0.4)" : "none",
                animation: selfEvolutionOn ? "selfEvoPulse 1.3s ease-in-out infinite" : "none",
                flexShrink: 0,
              }}
            />
            <span>Self-Evolution</span>
            <span
              style={{
                color: selfEvolutionOn ? "#ea580c" : "#94a3b8",
                flexShrink: 0,
                letterSpacing: 0.2,
              }}
            >
              {selfEvolutionReady ? (selfEvolutionOn ? "ON" : "OFF") : "..."}
            </span>
          </button>
          <style>
            {"@keyframes selfEvoPulse { 0%,100%{opacity:1} 50%{opacity:0.35} }"}
          </style>
          {latestScreenshot && !loading && (
            <button
              onClick={() => setAnnotating(true)}
              style={headerButtonStyle("#ea580c")}
              title="Annotate screenshot to give feedback"
            >
              Annotate
            </button>
          )}
          {!loading && sessionId && (
            <button
              onClick={handleSave}
              style={headerButtonStyle("#16a34a")}
              title="Save current scene"
            >
              Save
            </button>
          )}
          {!loading && sessionId && latestScreenshot && (
            <button
              onClick={handleShare}
              style={headerButtonStyle("#3b82f6")}
              title="Share to community gallery"
            >
              Share
            </button>
          )}
          {loading && (
            <button onClick={handleStop} style={headerButtonStyle("#dc2626")}>
              Stop
            </button>
          )}
          {!loading && sessionId && (
            <button
              onClick={handleReset}
              title="Reset conversation"
              style={headerButtonStyle("#64748b")}
            >
              Reset
            </button>
          )}
        </div>
      </div>

      {/* Skills panel */}
      <SkillsPanel
        selected={selectedSkills}
        onChange={setSelectedSkills}
        autoEnabled={autoSkillSelectionEnabled}
        onAutoEnabledChange={(enabled) => {
          setAutoSkillSelectionEnabled(Boolean(enabled));
          setAutoSelectionError("");
          if (!enabled) setAutoSelectingSkills(false);
        }}
        autoSelected={autoSelectedSkills}
      />

      {/* Messages */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "14px",
          display: "flex",
          flexDirection: "column",
          gap: 14,
        }}
      >
        {messages.map((msg) => (
          <ChatMessage key={msg.id} message={msg} />
        ))}
        {loading && lastMessage?.role === "user" && <TypingIndicator />}
        <div ref={scrollRef} />
      </div>

      {/* Quick suggestions */}
      {!loading && sessionId && turnCount > 0 && (
        <div
          style={{
            padding: "6px 14px",
            borderTop: "1px solid var(--line)",
            background: "var(--panel)",
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
          }}
        >
          {QUICK_SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              onClick={() => handleSend(suggestion)}
              style={{
                padding: "4px 11px",
                fontSize: 12,
                borderRadius: 999,
                border: "1px solid var(--line)",
                background: "var(--panel-2)",
                color: "var(--ink-2)",
                cursor: "pointer",
                fontWeight: 600,
                fontFamily: "inherit",
              }}
            >
              {suggestion}
            </button>
          ))}
        </div>
      )}

      {/* Input area */}
      <div
        style={{
          padding: "10px 14px",
          borderTop: "1px solid var(--line)",
          flexShrink: 0,
          background: "var(--panel)",
        }}
      >
        <div
          style={{
            display: "flex",
            gap: 8,
            alignItems: "flex-end",
            background: "var(--bg-tertiary)",
            border: "1px solid var(--line)",
            borderRadius: 10,
            padding: "8px 12px",
          }}
        >
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              sessionId
                ? "Refine the scene or describe changes..."
                : "Describe the city scene you want to generate…"
            }
            disabled={loading}
            rows={1}
            style={{
              flex: 1,
              background: "none",
              border: "none",
              outline: "none",
              color: "var(--ink)",
              fontSize: 13,
              resize: "none",
              lineHeight: 1.5,
              maxHeight: 160,
              overflow: "auto",
              fontFamily: "inherit",
              cursor: "text",
            }}
          />
          <button
            onClick={() => (loading ? handleStop() : handleSend())}
            disabled={!loading && !input.trim()}
            style={{
              flexShrink: 0,
              width: 34,
              height: 34,
              borderRadius: 10,
              border: "none",
              background: loading ? "rgba(255,95,99,0.15)" : input.trim() ? "var(--blue)" : "var(--panel-2)",
              color: loading ? "var(--red)" : input.trim() ? "#ffffff" : "var(--ink-3)",
              cursor: loading || input.trim() ? "pointer" : "default",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              fontSize: 15,
              boxShadow: input.trim() && !loading ? "0 4px 10px rgba(37,99,235,.25)" : "none",
            }}
          >
            {loading ? "⏹" : "▶"}
          </button>
        </div>
        <div style={{ marginTop: 5, fontSize: 12, color: "var(--ink-3)" }}>
          Enter to send · Shift+Enter for new line
          <span style={{ marginLeft: 8, color: autoSkillSelectionEnabled ? "var(--blue)" : "var(--ink-3)" }}>
            {autoSkillSelectionEnabled ? "Auto-select skills: on" : "Auto-select skills: off"}
            {autoSelectingSkills ? " (selecting...)" : ""}
          </span>          {activeSkills.length > 0 && (
            <span style={{ color: "var(--blue)", marginLeft: 8 }}>
              {activeSkills.length} skill{activeSkills.length > 1 ? "s" : ""} active
            </span>
          )}
          {autoSelectionError && (
            <span style={{ color: "var(--red)", marginLeft: 8 }}>{autoSelectionError}</span>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── KeyHint ─────────────────────────────────────────────────────────────────

function KeyHint({ icon, label }) {
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        gap: 4,
      }}
    >
      <div
        style={{
          background: "rgba(255,255,255,0.15)",
          border: "1px solid rgba(255,255,255,0.25)",
          borderRadius: 4,
          padding: "2px 8px",
          fontSize: 12,
          color: "#ffffff",
          fontFamily: "monospace",
          minWidth: 32,
          textAlign: "center",
        }}
      >
        {icon}
      </div>
      <div style={{ color: "var(--ink-3)", fontSize: 12 }}>{label}</div>
    </div>
  );
}

// ─── PixelStreamView ─────────────────────────────────────────────────────────

function PixelStreamView({ playerUrl }) {
  const iframeRef = useRef(null);
  const [active, setActive] = useState(false);
  const [showHints, setShowHints] = useState(false);

  const activate = useCallback(() => {
    setActive(true);
    setTimeout(() => iframeRef.current?.focus(), 100);
  }, []);

  useEffect(() => {
    if (!active) return;
    setShowHints(true);
    const timer = setTimeout(() => setShowHints(false), 4000);
    return () => clearTimeout(timer);
  }, [active]);

  return (
    <div style={{ width: "100%", height: "100%", position: "relative", background: "#000" }}>
      <iframe
        ref={iframeRef}
        src={playerUrl}
        style={{
          width: "100%",
          height: "100%",
          border: "none",
          display: "block",
          pointerEvents: active ? "auto" : "none",
        }}
        allow="pointer-lock *; fullscreen *; autoplay *; clipboard-read *; clipboard-write *"
        allowFullScreen
        tabIndex={0}
        title="UE Pixel Streaming"
      />

      {/* Activation overlay */}
      {!active && (
        <div
          onClick={activate}
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            background: "rgba(0,0,0,0.72)",
            cursor: "pointer",
            gap: 16,
          }}
        >
          <div style={{ fontSize: 48 }}>{ICONS.gamepad(48)}</div>
          <div style={{ color: "#ffffff", fontSize: 18, fontWeight: 600 }}>
            Click to Activate Stream
          </div>
          <div
            style={{
              color: "#cbd5e1",
              fontSize: 13,
              textAlign: "center",
              maxWidth: 320,
              lineHeight: 1.6,
            }}
          >
            Enables keyboard & mouse control.
            <br />
            The stream must load before interaction works.
          </div>
          <div
            style={{
              display: "flex",
              gap: 24,
              marginTop: 8,
              color: "var(--ink-3)",
              fontSize: 12,
            }}
          >
            <KeyHint icon={ICONS.mouse(14)} label="Click & drag to look" />
            <KeyHint icon={ICONS.keyboard(14)} label="WASD to move" />
            <KeyHint icon="Esc" label="Release mouse" />
          </div>
        </div>
      )}

      {/* Control hints (fade after activation) */}
      {active && showHints && (
        <div
          style={{
            position: "absolute",
            bottom: 12,
            left: "50%",
            transform: "translateX(-50%)",
            background: "rgba(0,0,0,0.75)",
            borderRadius: 8,
            padding: "10px 20px",
            display: "flex",
            gap: 20,
            pointerEvents: "none",
            border: "1px solid var(--line)",
          }}
        >
          <KeyHint icon={ICONS.mouse(14)} label="Click & drag to look" />
          <KeyHint icon="WASD" label="Move" />
          <KeyHint icon="Esc" label="Release mouse" />
          <KeyHint icon="F" label="Fullscreen" />
        </div>
      )}

      {/* Live badge */}
      {active && (
        <div
          style={{
            position: "absolute",
            top: 8,
            left: 8,
            background: "rgba(0,0,0,0.6)",
            color: "var(--green)",
            fontSize: 12,
            padding: "3px 8px",
            borderRadius: 4,
            pointerEvents: "none",
            display: "flex",
            alignItems: "center",
            gap: 5,
          }}
        >
          <span
            style={{
              display: "inline-block",
              width: 6,
              height: 6,
              borderRadius: "50%",
              background: "var(--green)",
              animation: "livebeat 2s ease-in-out infinite",
            }}
          />
          LIVE · SimWorld
          <style>
            {"@keyframes livebeat { 0%,100%{opacity:1} 50%{opacity:0.4} }"}
          </style>
        </div>
      )}

      {/* Deactivate button */}
      {active && (
        <button
          onClick={() => {
            setActive(false);
            setShowHints(false);
          }}
          style={{
            position: "absolute",
            top: 8,
            right: 8,
            padding: "3px 10px",
            fontSize: 12,
            background: "rgba(0,0,0,0.6)",
            border: "1px solid var(--line)",
            borderRadius: 4,
            color: "var(--ink-3)",
            cursor: "pointer",
          }}
        >
          Deactivate
        </button>
      )}
    </div>
  );
}

// ─── ScreenshotView ──────────────────────────────────────────────────────────

function ScreenshotView({ src, imgKey, onRefresh }) {
  const [loaded, setLoaded] = useState(false);
  const [errored, setErrored] = useState(false);

  // Reset load state when src or key changes
  useEffect(() => { setLoaded(false); setErrored(false); }, [src, imgKey]);

  if (src) {
    return (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "#000",
          overflow: "hidden",
        }}
      >
        <img
          key={imgKey}
          src={src}
          alt="UE viewport"
          style={{ maxWidth: "100%", maxHeight: "100%", objectFit: "contain", display: loaded ? "block" : "none" }}
          onLoad={() => setLoaded(true)}
          onError={() => { setErrored(true); setTimeout(() => onRefresh?.(), 1000); }}
        />
        {!loaded && !errored && (
          <span style={{ color: "var(--ink-3)", fontSize: 13 }}>Loading screenshot...</span>
        )}
        {errored && (
          <span style={{ color: "var(--ink-3)", fontSize: 13 }}>Retrying screenshot...</span>
        )}
      </div>
    );
  }

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        gap: 14,
        color: "var(--ink-3)",
      }}
    >
      <div style={{ fontSize: 44 }}>{ICONS.camera(44)}</div>
      <div style={{ fontSize: 13 }}>No screenshot yet</div>
      <div
        style={{
          fontSize: 12,
          color: "var(--ink-2)",
          textAlign: "center",
          maxWidth: 280,
        }}
      >
        Ask the agent to take a screenshot or make changes to the scene.
        <br />
        Screenshots auto-appear after agent actions.
      </div>
      <button
        onClick={onRefresh}
        style={{
          padding: "6px 18px",
          fontSize: 13,
          background: "var(--panel-2)",
          border: "1px solid var(--line)",
          borderRadius: 6,
          color: "var(--ink)",
          cursor: "pointer",
        }}
      >
        Fetch Latest
      </button>
    </div>
  );
}

// ─── ContextPanel ────────────────────────────────────────────────────────────

const EntityRow = React.memo(function EntityRow({ entity }) {
  const iconFn = CATEGORY_ICONS[entity.category] || ICONS.box;
  const loc = Array.isArray(entity.location) && entity.location.length >= 3
    ? entity.location.map((v) => Math.round(v)).join(", ")
    : null;
  return (
    <div style={{ display: "flex", alignItems: "flex-start", gap: 8, padding: "5px 0", borderBottom: "1px solid var(--line)" }}>
      <span style={{ fontSize: 14, flexShrink: 0, marginTop: 1, display: "inline-flex", alignItems: "center" }}>{iconFn(14)}</span>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
          <span style={{ color: "var(--ink)", fontSize: 12, fontWeight: 500 }}>{entity.name}</span>
          {entity.cls && (
            <span style={{ fontSize: 12, color: "var(--blue)", background: "#eff4ff", borderRadius: 3, padding: "1px 5px" }}>
              {entity.cls}
            </span>
          )}
        </div>
        {loc && <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 2 }}>@ ({loc})</div>}
      </div>
    </div>
  );
}); // React.memo(EntityRow)

function ContextPanel({ sessionId, refreshKey }) {
  // Use fine-grained scene context to avoid re-render on every SSE agent/chatlog push
  const scene = useScene();
  const state  = scene.objects?.length > 0 || scene.environment?.ready ? scene : null;
  const [lastUpdated, setLastUpdated] = useState(null);
  const MAX_DISPLAY = 150; // cap to avoid long lists causing layout thrash

  useEffect(() => { setLastUpdated(new Date()); }, [scene.round]);

  const containerStyle = {
    height: "100%", display: "flex", flexDirection: "column",
    background: "var(--bg)", color: "var(--ink)", overflow: "hidden",
  };
  const headerStyle = {
    padding: "10px 14px", borderBottom: "1px solid var(--line)",
    display: "flex", alignItems: "center", justifyContent: "space-between", flexShrink: 0,
  };
  const sectionStyle = { padding: "10px 14px 0" };
  const sectionTitleStyle = {
    fontSize: 12, fontWeight: 600, color: "var(--ink-3)",
    textTransform: "uppercase", letterSpacing: "0.05em", marginBottom: 6,
  };

  if (!state) {
    return (
      <div style={containerStyle}>
        <div style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center" }}>
          <span style={{ color: "var(--ink-3)", fontSize: 13 }}>
            {sessionId ? "No scene data yet — complete a round to populate." : "Start a chat session to see scene context."}
          </span>
        </div>
      </div>
    );
  }

  // Group objects by category, but cap per-category to avoid rendering 30k items
  const byCategory = {};
  let shown = 0;
  for (const o of scene.objects || []) {
    if (shown >= MAX_DISPLAY) break;
    (byCategory[o.category] = byCategory[o.category] || []).push(o);
    shown++;
  }
  const totalObjects = (scene.objects || []).length;
  const truncated = totalObjects > MAX_DISPLAY;

  return (
    <div style={containerStyle}>
      {/* Header */}
      <div style={headerStyle}>
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <span style={{ fontSize: 13, fontWeight: 600 }}>Scene Context</span>
          <span style={{ fontSize: 12, background: state.environment?.ready ? "#1a3a2a" : "#fff7ed",
            color: state.environment?.ready ? "#16a34a" : "#f59e0b",
            borderRadius: 3, padding: "1px 6px" }}>
            {state.environment?.ready ? "env ready" : "env not initialized"}
          </span>
          <span style={{ fontSize: 12, color: "var(--ink-3)" }}>round {state.round ?? 0}</span>
        </div>
        {lastUpdated && (
          <span style={{ fontSize: 12, color: "var(--ink-3)" }}>
            updated {lastUpdated.toLocaleTimeString()}
          </span>
        )}
      </div>

      <div style={{ flex: 1, overflowY: "auto", padding: "0 0 16px" }}>
        {/* Agents section */}
        <div style={sectionStyle}>
          <div style={sectionTitleStyle}>
            {ICONS.robot(13)} Agents &nbsp;<span style={{ color: "var(--blue)" }}>{(scene.agents || []).length}</span>
          </div>
          {(scene.agents || []).length === 0
            ? <div style={{ fontSize: 12, color: "var(--ink-3)", paddingBottom: 8 }}>No agents in scene</div>
            : (scene.agents || []).map((a) => <EntityRow key={a.name} entity={a} />)
          }
        </div>

        {/* Objects section, grouped by category (capped at MAX_DISPLAY for performance) */}
        <div style={{ ...sectionStyle, marginTop: 12 }}>
          <div style={sectionTitleStyle}>
            {ICONS.box(13)} Objects &nbsp;
            <span style={{ color: "var(--blue)" }}>{totalObjects}</span>
            {truncated && <span style={{ color: "#f59e0b", fontSize: 12, marginLeft: 4 }}>(showing {MAX_DISPLAY})</span>}
          </div>
          {totalObjects === 0
            ? <div style={{ fontSize: 12, color: "var(--ink-3)" }}>No objects in scene</div>
            : Object.entries(byCategory).map(([cat, items]) => (
                <div key={cat} style={{ marginBottom: 10 }}>
                  <div style={{ fontSize: 12, color: "var(--ink-3)", marginBottom: 4 }}>
                    {(CATEGORY_ICONS[cat] || ICONS.box)(11)} {cat}s ({items.length})
                  </div>
                  {items.map((o) => <EntityRow key={o.name} entity={o} />)}
                </div>
              ))
          }
          {truncated && (
            <div style={{ fontSize: 12, color: "#f59e0b", padding: "6px 0", borderTop: "1px solid var(--line)", marginTop: 4 }}>
              ⚠ {totalObjects - MAX_DISPLAY} more objects not shown — use the coding agent to query specific actors.
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── AgentPanel ──────────────────────────────────────────────────────────────

const AGENT_COLORS = ["#2563eb", "#16a34a", "#f59e0b", "#f778ba", "#bc8cff", "#ea580c", "#2563eb", "#56d364"];

async function sendAgentChat(agentName, message, sessionId, onEvent, signal) {
  console.log("[sendAgentChat] START", { agentName, message, sessionId });
  const response = await fetch(`${API_BASE}/agent-chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ agentName, message, sessionId }),
    signal,
  });
  console.log("[sendAgentChat] fetch response", { status: response.status, ok: response.ok, hasBody: !!response.body });
  if (!response.ok || !response.body) {
    const err = await response.json().catch(() => ({ error: response.statusText }));
    console.error("[sendAgentChat] ERROR response", err);
    throw new Error(err.error || `Server error: ${response.status}`);
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let lastDataTime = Date.now();
  const IDLE_TIMEOUT = 210000; // 3.5 min — allow time for long MCP tool calls

  for (;;) {
    const readPromise = reader.read();
    const timeoutPromise = new Promise((_, reject) => {
      const check = setInterval(() => {
        if (Date.now() - lastDataTime > IDLE_TIMEOUT) {
          clearInterval(check);
          reader.cancel();
          reject(new Error("Agent response timeout"));
        }
      }, 5000);
      readPromise.then(() => clearInterval(check)).catch(() => clearInterval(check));
    });

    let result;
    try {
      result = await Promise.race([readPromise, timeoutPromise]);
    } catch {
      onEvent({ type: "done", data: { isError: true, text: "Agent timed out." } });
      break;
    }

    const { done, value } = result;
    if (done) break;

    lastDataTime = Date.now();
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const lines = chunk.split("\n");
      let eventType = "message";
      let data = "";
      for (const line of lines) {
        if (line.startsWith("event: ")) eventType = line.slice(7).trim();
        if (line.startsWith("data: ")) data = line.slice(6);
      }
      if (data) {
        try { onEvent({ type: eventType, data: JSON.parse(data) }); } catch {}
      }
    }
  }
}

function AgentCard({ agent, sessionId, pieActive, colorIdx, onExpand }) {
  const [status, setStatus] = useState("idle");
  const [thought, setThought] = useState(""); // Current reasoning text
  const [actions, setActions] = useState([]); // [{tool, ok}]
  const [input, setInput] = useState("");
  const abortRef = useRef(null);
  const activityRef = useRef(null);
  const color = AGENT_COLORS[colorIdx % AGENT_COLORS.length];

  // Get activities + messages from unified poll
  const pollData = usePoll();
  const pastActivities = pollData.activities?.[agent.name] || [];
  // Messages where this agent was mentioned or targeted
  const agentMessages = useMemo(() => {
    return (pollData.chatLog || []).filter(m =>
      m.from !== agent.name && (m.to === agent.name || m.to === "all" || !m.to)
    ).slice(-5);
  }, [pollData.chatLog, agent.name]);

  const handleSend = useCallback(async (text) => {
    if (!text.trim() || status === "running") return;
    setInput("");
    setStatus("running");
    setThought("");
    setActions([]);

    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await sendAgentChat(agent.name, text, sessionId, (event) => {
        switch (event.type) {
          case "text":      setThought(prev => prev + event.data.delta); break;
          case "thinking":  setThought(prev => prev + event.data.delta); break;
          case "tool_start": setActions(prev => [...prev, { tool: event.data.displayName, ok: null }]); break;
          case "tool_result": setActions(prev => prev.map((a, i) => i === prev.length - 1 ? { ...a, ok: !event.data.isError } : a)); break;
          default: break;
        }
      }, controller.signal);
      setStatus("done");
      setThought("");
      setActions([]);
    } catch (err) {
      if (err.name !== "AbortError") {
        setThought(prev => prev || `Error: ${err.message}`);
        setStatus("error");
      } else {
        setStatus("idle");
      }
    } finally { abortRef.current = null; }
  }, [agent.name, sessionId, status, pieActive]);

  const handleStop = () => {
    abortRef.current?.abort();
    fetch(`${API_BASE}/agent-stop`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ agentName: agent.name }) }).catch(() => {});
    setStatus("idle");
  };

  useEffect(() => { if (activityRef.current) activityRef.current.scrollTop = activityRef.current.scrollHeight; }, [thought, actions]);

  const loc = Array.isArray(agent.location) && agent.location.length >= 3
    ? agent.location.map(v => Math.round(v)).join(", ") : null;

  // Yaw → compass heading
  const heading = useMemo(() => {
    const rot = agent.rotation;
    if (!Array.isArray(rot) || rot.length < 3) return null;
    const yaw = ((rot[1] % 360) + 360) % 360;
    const dirs = ["N","NE","E","SE","S","SW","W","NW"];
    const dir = dirs[Math.round(yaw / 45) % 8];
    return `${dir} ${Math.round(yaw)}°`;
  }, [agent.rotation]);

  // Sync server-side status into local state when not actively streaming
  const svrStatus = agent.status;
  useEffect(() => {
    if (status !== "running") setStatus(svrStatus === "running" ? "running" : status);
  }, [svrStatus]); // eslint-disable-line

  // Current action from server (for agents triggered externally)
  const liveAction = status === "running"
    ? (actions[actions.length - 1]?.tool || agent.currentAction)
    : (agent.lastAction || null);

  const statusColors = { idle: "#64748b", running: "#f59e0b", done: "#16a34a", error: "#dc2626" };

  // Render a single activity (ReAct format)
  const renderActivity = (act, isLive) => {
    const t = act.thought || act.response || "";
    const acts = isLive ? actions : (act.actions || []);
    return (
      <div style={{ fontSize: 12, lineHeight: "1.5" }}>
        {/* Thought */}
        {t && (
          <div style={{ color: "var(--ink)", whiteSpace: "pre-wrap", marginBottom: 4 }}>
            <span style={{ color: "var(--ink-3)", fontWeight: 600 }}>Thought: </span>{t.slice(0, 500)}
          </div>
        )}
        {/* Actions */}
        {acts.length > 0 && acts.map((a, i) => (
          <div key={i} style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 2, paddingLeft: 8 }}>
            <span style={{ color: a.ok === null ? "#f59e0b" : a.ok ? "#16a34a" : "#dc2626", fontWeight: 600 }}>
              {a.ok === null ? "..." : a.ok ? "ok" : "err"}
            </span>
            <span style={{ color: "var(--blue)" }}>{a.tool || a.name}</span>
          </div>
        ))}
      </div>
    );
  };

  // ── Collapsed card ──────────────────────────────────────────────────────────
  return (
    <div
      onClick={() => onExpand?.(agent)}
      style={{
        border: `1px solid ${color}33`, borderRadius: 8, background: "var(--panel,#ffffff)",
        display: "flex", flexDirection: "column", overflow: "hidden", minWidth: 0,
        cursor: "pointer", transition: "box-shadow 0.15s, border-color 0.15s",
      }}
      onMouseEnter={e => { e.currentTarget.style.borderColor = color + "88"; e.currentTarget.style.boxShadow = `0 2px 10px ${color}18`; }}
      onMouseLeave={e => { e.currentTarget.style.borderColor = color + "33"; e.currentTarget.style.boxShadow = "none"; }}
    >
      {/* Header row */}
      <div style={{ padding: "8px 10px", display: "flex", alignItems: "center", gap: 6, background: `${color}0a` }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: statusColors[status], flexShrink: 0,
          boxShadow: status === "running" ? `0 0 0 2px ${statusColors[status]}44` : "none" }} />
        <span style={{ fontSize: 12, fontWeight: 700, color, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{agent.name}</span>
        <span style={{ fontSize: 12, color: "var(--ink-3)", background: "var(--bg-tertiary)", borderRadius: 3, padding: "1px 5px", flexShrink: 0 }}>{agent.cls || "?"}</span>
        <span style={{ fontSize: 12, color: "var(--ink-3)" }}>›</span>
      </div>

      {/* Quick info */}
      <div style={{ padding: "5px 10px 7px", fontSize: 12 }}>
        {/* Position + heading row */}
        <div style={{ display: "flex", gap: 6, alignItems: "baseline" }}>
          {loc ? (
            <span style={{ color: "var(--ink-3)", fontFamily: "monospace", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{loc}</span>
          ) : (
            <span style={{ color: "var(--ink-3)", fontStyle: "italic" }}>location unknown</span>
          )}
          {heading && (
            <span style={{ color: "var(--ink-3)", flexShrink: 0, fontFamily: "monospace" }}>{heading}</span>
          )}
        </div>
        {/* Live action / last action */}
        {(liveAction || thought || actions.length > 0) && (
          <div style={{ color: "var(--ink-3)", marginTop: 3, display: "flex", alignItems: "center", gap: 4 }}>
            {status === "running" && <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#f59e0b", animation: "pulse 1s ease-in-out infinite", flexShrink: 0 }}/>}
            <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {thought ? thought.slice(0, 50) + (thought.length > 50 ? "…" : "") : (liveAction || actions[actions.length-1]?.tool)}
            </span>
          </div>
        )}
        {pastActivities.length > 0 && !thought && (
          <div style={{ color: "var(--ink-3)", marginTop: 2, fontSize: 12 }}>
            {pastActivities.length} past action{pastActivities.length > 1 ? "s" : ""}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── AgentTrajectoryView ─────────────────────────────────────────────────────

function AgentTrajectoryView({ agentName, color, liveState }) {
  // Primary source: SSE trajectoryPreview (free, no extra request, updates with SSE)
  // Full trajectory loaded on demand only.
  const [fullTraj, setFullTraj] = useState(null);  // null = not yet loaded
  const [loadingFull, setLoadingFull] = useState(false);

  const loadFull = useCallback(() => {
    setLoadingFull(true);
    fetch(`${API_BASE}/agent-trajectory/${encodeURIComponent(agentName)}`)
      .then(r => r.json())
      .then(d => { setFullTraj(d.trajectory || []); })
      .catch(()=>{})
      .finally(() => setLoadingFull(false));
  }, [agentName]);

  // Use full trajectory if loaded, else fall back to SSE preview
  const traj = fullTraj ?? (liveState?.trajectoryPreview || []);

  const collisions = liveState?.recentCollisions || [];
  const envEvents  = liveState?.envFeedback || [];

  if (traj.length < 2) return (
    <div style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", gap:8, color:"var(--ink-3)", fontSize:12 }}>
      <div style={{ opacity:0.3, display:"flex", justifyContent:"center" }}>{ICONS.map(28)}</div>
      <div>No trajectory data yet — agent needs to move</div>
    </div>
  );

  // Compute bounding box for normalization
  const xs = traj.map(p=>p.loc[0]), ys = traj.map(p=>p.loc[1]);
  const minX=Math.min(...xs), maxX=Math.max(...xs), minY=Math.min(...ys), maxY=Math.max(...ys);
  const W=420, H=280, pad=24;
  const rangeX = maxX-minX || 1, rangeY = maxY-minY || 1;
  const scale = Math.min((W-2*pad)/rangeX, (H-2*pad)/rangeY);
  const toSvg = (x,y) => [pad+(x-minX)*scale, H-pad-(y-minY)*scale];

  const pathD = traj.map((p,i) => {
    const [sx,sy] = toSvg(p.loc[0], p.loc[1]);
    return `${i===0?'M':'L'} ${sx.toFixed(1)} ${sy.toFixed(1)}`;
  }).join(' ');

  // Collision positions: from recentCollisions AND from trajectory points flagged hit:true
  const collisionPts = [
    ...collisions.map(c => c.loc ? toSvg(c.loc[0],c.loc[1]) : null),
    ...traj.filter(p => p.hit && p.loc).map(p => toSvg(p.loc[0],p.loc[1])),
  ].filter(Boolean);

  const last = traj[traj.length-1];
  const [lastX, lastY] = toSvg(last.loc[0], last.loc[1]);
  const yaw = last.rot ? ((last.rot[1]%360)+360)%360 : 0;
  const arrowRad = yaw * Math.PI / 180;

  const isPreview = !fullTraj;
  const totalPts  = liveState?.trajectoryLength ?? traj.length;

  return (
    <div style={{ flex:1, overflow:"auto", padding:12 }}>
      {/* Header: preview vs full indicator + load button */}
      <div style={{ display:"flex", alignItems:"center", gap:6, marginBottom:6, fontSize:12, color:"var(--ink-3)" }}>
        <span>{isPreview ? `Preview (last ${traj.length})` : `Full (${traj.length} pts)`} of {totalPts} total</span>
        {isPreview && totalPts > traj.length && (
          <button onClick={loadFull} disabled={loadingFull}
            style={{ fontSize:12, padding:"2px 7px", borderRadius:4, border:"1px solid var(--line)",
              background:"none", cursor:"pointer", color:"var(--blue)" }}>
            {loadingFull ? "Loading…" : `↓ Load all ${totalPts}`}
          </button>
        )}
      </div>
      {/* SVG top-down trajectory map */}
      <svg width={W} height={H} style={{ background:"var(--bg)", borderRadius:8, border:"1px solid var(--line)", display:"block", margin:"0 auto" }}>
        {/* Grid lines */}
        {[0.25,0.5,0.75].map(f => (
          <line key={f} x1={pad} y1={H-pad-f*(H-2*pad)} x2={W-pad} y2={H-pad-f*(H-2*pad)}
            stroke="var(--line)" strokeWidth={0.5} strokeDasharray="3,3" />
        ))}
        {/* Trajectory path */}
        <path d={pathD} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" opacity={0.85} />
        {/* Start point */}
        {(() => { const [sx,sy]=toSvg(traj[0].loc[0],traj[0].loc[1]); return (
          <circle cx={sx} cy={sy} r={5} fill="#22c55e" stroke="#fff" strokeWidth={1.5} />
        ); })()}
        {/* Collision markers */}
        {collisionPts.map(([cx,cy],i) => (
          <circle key={i} cx={cx} cy={cy} r={6} fill="#dc2626" opacity={0.7} />
        ))}
        {/* Current agent position with heading arrow */}
        <circle cx={lastX} cy={lastY} r={7} fill={color} stroke="#fff" strokeWidth={2} />
        <line x1={lastX} y1={lastY}
          x2={lastX + Math.cos(arrowRad-Math.PI/2)*14}
          y2={lastY + Math.sin(arrowRad-Math.PI/2)*14}
          stroke="#fff" strokeWidth={2} strokeLinecap="round" />
        {/* Legend */}
        <circle cx={12} cy={H-10} r={4} fill="#22c55e" /><text x={20} y={H-7} fontSize={8} fill="var(--ink-3)">Start</text>
        <circle cx={55} cy={H-10} r={4} fill={color} /><text x={63} y={H-7} fontSize={8} fill="var(--ink-3)">Current</text>
        {collisionPts.length > 0 && <>
          <circle cx={105} cy={H-10} r={4} fill="#dc2626" />
          <text x={113} y={H-7} fontSize={8} fill="var(--ink-3)">Collision ({collisionPts.length})</text>
        </>}
      </svg>

      {/* Stats row */}
      <div style={{ display:"flex", gap:12, marginTop:10, fontSize:12 }}>
        <div style={{ flex:1, padding:8, background:"var(--bg)", borderRadius:7, border:"1px solid var(--line)", textAlign:"center" }}>
          <div style={{ fontSize:18, fontWeight:700, color }}>{traj.length}</div>
          <div style={{ fontSize:12, color:"var(--ink-3)" }}>Track Points</div>
        </div>
        <div style={{ flex:1, padding:8, background:"var(--bg)", borderRadius:7, border:"1px solid var(--line)", textAlign:"center" }}>
          <div style={{ fontSize:18, fontWeight:700, color: collisions.length>0?"#dc2626":color }}>{liveState?.collisionCount||0}</div>
          <div style={{ fontSize:12, color:"var(--ink-3)" }}>Total Collisions</div>
        </div>
        <div style={{ flex:1, padding:8, background:"var(--bg)", borderRadius:7, border:"1px solid var(--line)", textAlign:"center" }}>
          <div style={{ fontSize:18, fontWeight:700, color }}>{liveState?.totalTurns||0}</div>
          <div style={{ fontSize:12, color:"var(--ink-3)" }}>Turns</div>
        </div>
        <div style={{ flex:1, padding:8, background:"var(--bg)", borderRadius:7, border:"1px solid var(--line)", textAlign:"center" }}>
          <div style={{ fontSize:18, fontWeight:700, color }}>{Math.round((liveState?.speed||0)/100)}</div>
          <div style={{ fontSize:12, color:"var(--ink-3)" }}>m/s</div>
        </div>
      </div>

      {/* Recent collisions */}
      {collisions.length > 0 && (
        <div style={{ marginTop:10 }}>
          <div style={{ fontSize:12, fontWeight:700, color:"#dc2626", marginBottom:4, textTransform:"uppercase" }}>Recent Collisions</div>
          {collisions.slice(-5).map((c,i) => (
            <div key={i} style={{ fontSize:12, color:"var(--ink-2)", padding:"3px 6px", background:"rgba(220,38,38,.06)", borderRadius:4, marginBottom:2, borderLeft:"2px solid #dc2626" }}>
              {new Date(c.ts).toLocaleTimeString()} — hit: {c.overlapping?.join(", ")||"unknown"}
              {c.impulse > 0 && <span style={{ color:"#f59e0b", marginLeft:6 }}>impulse {Math.round(c.impulse)} N</span>}
            </div>
          ))}
        </div>
      )}

      {/* Environment feedback */}
      {envEvents.length > 0 && (
        <div style={{ marginTop:10 }}>
          <div style={{ fontSize:12, fontWeight:700, color:"var(--ink-3)", marginBottom:4, textTransform:"uppercase" }}>Nearby Environment</div>
          {envEvents.slice(-3).map((ev,i) => (
            <div key={i} style={{ fontSize:12, color:"var(--ink-2)", padding:"3px 6px", background:"var(--bg)", borderRadius:4, marginBottom:2 }}>
              {new Date(ev.ts).toLocaleTimeString()} — {ev.nearby?.length||0} objects within 3m: {ev.nearby?.slice(0,3).map(n=>n.name).join(", ")}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── AgentDetailPanel — expanded view with camera ────────────────────────────

// Yaw → compass direction label
function yawToCompass(yaw) {
  const dirs = ["N","NE","E","SE","S","SW","W","NW"];
  return dirs[Math.round(((yaw % 360) + 360) % 360 / 45) % 8];
}

function AgentDetailPanel({ agent, sessionId, pieActive, colorIdx, onClose }) {
  const color = AGENT_COLORS[colorIdx % AGENT_COLORS.length];
  const [activeTab, setActiveTab]     = useState("camera");
  const [camImg, setCamImg]           = useState(null);
  const [camLoading, setCamLoading]   = useState(false);
  const [camError, setCamError]       = useState(null);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [input, setInput]             = useState("");
  const [sending, setSending]         = useState(false);
  // Drag state for floating window
  const [winPos, setWinPos] = useState({ x: window.innerWidth - 540, y: 80 });
  const camIntervalRef = useRef(null);
  const activityRef    = useRef(null);

  // Use SSE data (usePoll) as primary source — no separate 1s polling.
  // SSE pushes every 3s; sessions already include location/rotation/speed/collisions/trajectory.
  const pollData = usePoll();
  const pastActivities = pollData.activities?.[agent.name] || [];

  // Derive liveState from SSE sessions (free, no extra request)
  const liveState = useMemo(() => {
    const s = (pollData.sessions || []).find(s => s.agentName === agent.name);
    return s || null;
  }, [pollData.sessions, agent.name]);

  // Draggable window handlers
  const startDrag = useCallback((e) => {
    e.preventDefault();
    const startX = e.clientX - winPos.x, startY = e.clientY - winPos.y;
    const onMove = (ev) => setWinPos({ x: ev.clientX - startX, y: ev.clientY - startY });
    const onUp   = () => { document.removeEventListener("mousemove", onMove); document.removeEventListener("mouseup", onUp); };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [winPos]);
  const agentMessages  = useMemo(() =>
    (pollData.chatLog || []).filter(m =>
      m.from !== agent.name && (m.to === agent.name || m.to === "all" || !m.to)
    ).slice(-20),
  [pollData.chatLog, agent.name]);

  // Use liveState from SSE; fall back to agent prop until SSE syncs (max ~3s)
  const isStillSyncing = !liveState;
  const loc = liveState?.location ?? (Array.isArray(agent.location) ? agent.location : null);
  const rot = liveState?.rotation ?? null;
  const liveStatus = liveState?.status ?? agent.status ?? "idle";
  const currentAction = liveState?.currentAction ?? null;
  const lastAction    = liveState?.lastAction ?? null;

  // Camera: /api/agent-camera renders from agent's own POV (eye-level, forward-facing)
  // Backend handles: Python set_level_viewport_camera_info → UCV vget /camera/0/lit
  const focusAndShoot = useCallback(async () => {
    setCamLoading(true); setCamError(null);
    try {
      const d = await fetch(`${API_BASE}/agent-camera/${encodeURIComponent(agent.name)}`).then(r => r.json());
      if (d.dataUrl) {
        setCamImg(d.dataUrl);
      } else {
        throw new Error("No camera data available");
      }
    } catch(e) { setCamError(e.message); } finally { setCamLoading(false); }
  }, [agent.name]);

  // Auto-refresh camera — 4s when tab active (images are expensive)
  useEffect(() => {
    if (activeTab !== "camera" || !autoRefresh) {
      clearInterval(camIntervalRef.current); return;
    }
    focusAndShoot();
    camIntervalRef.current = setInterval(focusAndShoot, 4000);
    return () => clearInterval(camIntervalRef.current);
  }, [activeTab, autoRefresh, focusAndShoot]);

  // Scroll activity log
  useEffect(() => {
    if (activityRef.current) activityRef.current.scrollTop = activityRef.current.scrollHeight;
  }, [pastActivities, agentMessages]);

  const handleSend = async () => {
    const text = input.trim();
    if (!text || sending) return;
    setInput("");
    setSending(true);
    try {
      await fetch(`${API_BASE}/agent-broadcast`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, target: agent.name }),
      });
    } catch {}
    setSending(false);
  };

  const statusColors = { idle:"#64748b", running:"#f59e0b", done:"#16a34a", error:"#dc2626" };

  return (
    <div style={{
      position: "fixed", left: winPos.x, top: winPos.y,
      width: 480, height: 560, zIndex: 200,
      background: "var(--panel)", borderRadius: 12,
      border: `1px solid ${color}55`,
      boxShadow: "0 8px 32px rgba(0,0,0,0.35)",
      display: "flex", flexDirection: "column", overflow: "hidden",
    }}>
      {/* ── Draggable header ── */}
      <div onMouseDown={startDrag} style={{
        padding: "8px 10px", borderBottom: "1px solid var(--line)",
        display: "flex", alignItems: "center", gap: 6, flexShrink: 0,
        background: `${color}12`, cursor: "grab", userSelect: "none",
      }}>
        <span style={{ width: 8, height: 8, borderRadius: "50%", background: statusColors[liveStatus],
          boxShadow: liveStatus==="running" ? `0 0 0 3px ${statusColors[liveStatus]}44` : "none",
          animation: liveStatus==="running" ? "ps-pulse 1s infinite" : "none", flexShrink:0 }}/>
        <span style={{ fontSize: 13, fontWeight: 700, color }}>{agent.name}</span>
        <span style={{ fontSize: 12, color:"var(--ink-3)", background:"var(--bg)", borderRadius:4, padding:"1px 5px" }}>{agent.cls}</span>
        {/* Syncing indicator — shows briefly until first SSE push arrives */}
        {isStillSyncing && <span style={{ fontSize:11, color:"var(--ink-3)", fontStyle:"italic" }}>syncing…</span>}
        {/* Live position */}
        {loc && <span style={{ fontSize:12, color:"var(--ink-3)", fontFamily:"monospace", marginLeft:2 }}>
          {loc.map(v=>Math.round(v)).join(",")}
        </span>}
        {/* Heading */}
        {rot && <span style={{ fontSize:12, color, fontWeight:700, marginLeft:2 }}>
          {yawToCompass(rot[1])} {Math.round(((rot[1]%360)+360)%360)}°
        </span>}
        {/* Current action */}
        {currentAction && <span style={{ fontSize:12, color:"#f59e0b", background:"rgba(245,158,11,.12)",
          borderRadius:4, padding:"1px 5px", maxWidth:120, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap" }}>
          {ICONS.zap(11)} {currentAction}
        </span>}
        {!currentAction && lastAction && <span style={{ fontSize:12, color:"var(--ink-3)",
          background:"var(--bg)", borderRadius:4, padding:"1px 5px" }}>
          last: {lastAction}
        </span>}
        <div style={{ flex:1 }} />
        <button onClick={onClose} onMouseDown={e=>e.stopPropagation()}
          style={{ background:"none", border:"1px solid var(--line)", borderRadius:6,
            width:22, height:22, cursor:"pointer", fontSize:12, color:"var(--ink-3)",
            display:"flex", alignItems:"center", justifyContent:"center" }}>×</button>
      </div>

      {/* ── Stats bar (speed, collisions) ── */}
      <div style={{ padding:"4px 10px", background:"var(--bg)", display:"flex", gap:12,
        fontSize:12, color:"var(--ink-3)", flexShrink:0, borderBottom:"1px solid var(--line)" }}>
        {liveState?.speed > 0 && (
          <span style={{ display:"inline-flex", alignItems:"center", gap:3 }}>{ICONS.zap(11)} {Math.round((liveState.speed||0)/100)} m/s</span>
        )}
        <span style={{ color: liveState?.collisionCount > 0 ? "#dc2626" : "var(--ink-3)" }}>
          <span style={{ display:"inline-flex", alignItems:"center", gap:3 }}>{ICONS.collision(11)} {liveState?.collisionCount||0} collisions</span>
        </span>
        <span>{liveState?.totalTurns||0} turns</span>
        {liveState?.totalCostUsd > 0 && (
          <span>${(liveState.totalCostUsd||0).toFixed(3)}</span>
        )}
      </div>

      {/* ── Tab bar ── */}
      <div style={{ display: "flex", gap: 0, borderBottom: "1px solid var(--line)", flexShrink: 0, background: "var(--panel)" }}>
        {[
          { id: "camera",     label: "Camera"     },
          { id: "trajectory", label: "Trajectory" },
          { id: "activity",   label: "Activity"   },
          { id: "chat",       label: "Chat"       },
        ].map(t => (
          <button key={t.id} onClick={() => setActiveTab(t.id)} style={{
            flex: 1, padding: "7px 2px", border: "none",
            borderBottom: `2px solid ${activeTab === t.id ? color : "transparent"}`,
            background: "none", cursor: "pointer",
            fontSize: 12, fontWeight: activeTab === t.id ? 700 : 400,
            color: activeTab === t.id ? color : "var(--ink-3)",
          }}>{t.label}</button>
        ))}
      </div>

      {/* ── Camera tab ── */}
      {activeTab === "camera" && (
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          {/* Camera controls */}
          <div style={{ padding: "6px 10px", display: "flex", alignItems: "center", gap: 6, borderBottom: "1px solid var(--line)", flexShrink: 0 }}>
            <label style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 12, cursor: "pointer", color: "var(--ink-3)" }}>
              <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} style={{ accentColor: color }}/>
              Auto (4s)
            </label>
            <button onClick={focusAndShoot} disabled={camLoading} style={{
              padding: "3px 10px", borderRadius: 6, border: `1px solid ${color}44`,
              background: `${color}11`, color, cursor: camLoading ? "wait" : "pointer",
              fontSize: 12, fontWeight: 600, opacity: camLoading ? 0.6 : 1,
            }}>
              {camLoading ? "Capturing…" : "Capture"}
            </button>
            {!loc && <span style={{ fontSize: 12, color: "var(--red)" }}>No location — no camera</span>}
          </div>

          {/* Camera image */}
          <div style={{ flex: 1, background: "#0b1220", position: "relative", overflow: "hidden" }}>
            {camImg ? (
              <img src={camImg} alt={`${agent.name} view`} style={{
                width: "100%", height: "100%", objectFit: "contain", display: "block",
              }}/>
            ) : camError ? (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 8 }}>
                <div style={{ color: "var(--ink-3)", opacity: 0.6 }}>{ICONS.camera(28)}</div>
                <div style={{ color: "var(--red)", fontSize: 12 }}>{camError}</div>
                <button onClick={focusAndShoot} style={{ padding: "5px 14px", borderRadius: 6, border: "1px solid var(--blue)", background: "rgba(37,99,235,.15)", color: "#93c5fd", cursor: "pointer", fontSize: 12 }}>Retry</button>
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", height: "100%", gap: 10 }}>
                {camLoading ? (
                  <>
                    <div style={{ width: 32, height: 32, borderRadius: "50%", border: "3px solid rgba(37,99,235,.2)", borderTopColor: "var(--blue)", animation: "ps-spin 0.9s linear infinite" }}/>
                    <div style={{ color: "var(--ink-3)", fontSize: 12 }}>Focusing camera…</div>
                  </>
                ) : (
                  <>
                    <div style={{ color: "var(--ink-3)", opacity: 0.5 }}>{ICONS.camera(28)}</div>
                    <div style={{ color: "var(--ink-2)", fontSize: 12 }}>Click "Capture" to take a screenshot</div>
                  </>
                )}
              </div>
            )}
            {/* Loading overlay */}
            {camLoading && camImg && (
              <div style={{ position: "absolute", inset: 0, background: "rgba(11,18,32,.5)", display: "flex", alignItems: "center", justifyContent: "center" }}>
                <div style={{ width: 24, height: 24, borderRadius: "50%", border: "2px solid rgba(255,255,255,.2)", borderTopColor: "#fff", animation: "ps-spin 0.9s linear infinite" }}/>
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── Trajectory tab ── */}
      {activeTab === "trajectory" && (
        <AgentTrajectoryView agentName={agent.name} color={color} liveState={liveState} />
      )}

      {/* ── Activity tab ── */}
      {activeTab === "activity" && (
        <div ref={activityRef} style={{ flex: 1, overflowY: "auto", padding: 10 }}>
          {agentMessages.length > 0 && (
            <div style={{ marginBottom: 10 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: "var(--ink-3)", letterSpacing: ".08em", marginBottom: 4 }}>INCOMING MESSAGES</div>
              {agentMessages.map((m, i) => (
                <div key={i} style={{ marginBottom: 5, fontSize: 12, borderLeft: `2px solid ${color}`, paddingLeft: 7, color: "var(--ink-2)" }}>
                  <span style={{ fontWeight: 600, color }}>{m.from}</span>: {m.text}
                </div>
              ))}
            </div>
          )}
          {pastActivities.length === 0 && agentMessages.length === 0 ? (
            <div style={{ color: "var(--ink-3)", fontSize: 12, fontStyle: "italic", textAlign: "center", paddingTop: 20 }}>No activity yet</div>
          ) : (
            pastActivities.map((act, i) => {
              const t = act.thought || act.response || "";
              const acts = act.actions || [];
              return (
                <div key={i} style={{ marginBottom: 10, padding: "8px 10px", borderRadius: 7, background: "var(--bg)", border: "1px solid var(--line)" }}>
                  <div style={{ fontSize: 12, color: "var(--ink-3)", marginBottom: 4 }}>Turn {i + 1}</div>
                  {t && <div style={{ fontSize: 12, color: "var(--ink-2)", marginBottom: 4, lineHeight: 1.5 }}>{t.slice(0, 200)}{t.length > 200 ? "…" : ""}</div>}
                  {acts.map((a, j) => (
                    <div key={j} style={{ display: "flex", alignItems: "center", gap: 5, fontSize: 12 }}>
                      <span style={{ color: a.ok ? "#16a34a" : "#dc2626" }}>{a.ok ? "✓" : "✗"}</span>
                      <span style={{ color: "var(--blue)", fontFamily: "monospace" }}>{a.tool || a.name}</span>
                    </div>
                  ))}
                </div>
              );
            })
          )}
        </div>
      )}

      {/* ── Chat tab ── */}
      {activeTab === "chat" && (
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <div style={{ padding: "8px 10px", fontSize: 12, color: "var(--ink-3)", borderBottom: "1px solid var(--line)", flexShrink: 0 }}>
            Send a direct command to <strong style={{ color }}>{agent.name}</strong>
          </div>
          <div ref={activityRef} style={{ flex: 1, overflowY: "auto", padding: 10 }}>
            {agentMessages.map((m, i) => (
              <div key={i} style={{ marginBottom: 8, fontSize: 12, lineHeight: 1.5 }}>
                <div style={{ fontWeight: 600, color: "var(--ink-3)", marginBottom: 2 }}>{m.from}</div>
                <div style={{ color: "var(--ink-2)", background: "var(--bg)", borderRadius: 6, padding: "5px 8px" }}>{m.text}</div>
              </div>
            ))}
          </div>
          <div style={{ padding: "8px 10px", borderTop: "1px solid var(--line)", display: "flex", gap: 6, flexShrink: 0 }}>
            <textarea
              value={input}
              onChange={e => setInput(e.target.value)}
              onKeyDown={e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); handleSend(); } }}
              placeholder={`Command ${agent.name}… (Enter to send)`}
              rows={2}
              style={{
                flex: 1, resize: "none", borderRadius: 8, border: "1px solid var(--line)",
                background: "var(--bg)", padding: "6px 10px", fontSize: 12, color: "var(--ink-2)",
                outline: "none", fontFamily: "inherit",
              }}
            />
            <button onClick={handleSend} disabled={!input.trim() || sending} style={{
              padding: "6px 14px", borderRadius: 8, border: "none",
              background: input.trim() && !sending ? color : "var(--line)",
              color: "#fff", cursor: input.trim() && !sending ? "pointer" : "default",
              fontSize: 12, fontWeight: 600, alignSelf: "flex-end",
            }}>Send</button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Aggregate Panel: tabs for Overview / Testbed / Chat ─────────────────────

function AgentAggregatePanelTabs({ agents, sessionId }) {
  const [tab, setTab] = useState("overview");
  const [trackName, setTrackName] = useState("");
  const [trackMsg, setTrackMsg] = useState("");

  const handleTrack = async () => {
    const name = trackName.trim();
    if (!name) return;
    await fetch(`${API_BASE}/agent-track`, {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ name }),
    });
    setTrackName("");
    setTrackMsg(`Tracking: ${name}`);
    setTimeout(() => setTrackMsg(""), 3000);
  };

  const tabs = [
    { id:"overview", label:"Overview" },
    { id:"testbed",  label:"Testbed"  },
    { id:"chat",     label:"Comm"     },
  ];
  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", background:"var(--bg)" }}>
      {/* Track agent by name (auto-discovery runs server-side every 10s) */}
      <div style={{ display:"flex", alignItems:"center", gap:4, padding:"4px 8px",
        borderBottom:"1px solid var(--line)", flexShrink:0, background:"var(--panel)" }}>
        <input value={trackName} onChange={e=>setTrackName(e.target.value)}
          onKeyDown={e=>e.key==="Enter"&&handleTrack()}
          placeholder="Track agent by name…"
          style={{ flex:1, padding:"4px 8px", fontSize:"var(--fs-body)", border:"1px solid var(--line)",
            borderRadius:5, background:"var(--bg)", color:"var(--ink-1)", outline:"none",
            fontFamily:"inherit" }} />
        <button onClick={handleTrack} style={{ padding:"4px 10px", borderRadius:5, border:"none",
          background:"var(--blue)", color:"#fff", cursor:"pointer",
          fontSize:"var(--fs-body)", fontFamily:"inherit" }}>Track</button>
        {trackMsg && <span style={{ fontSize:12, color:"var(--blue)", whiteSpace:"nowrap" }}>{trackMsg}</span>}
      </div>

      <div style={{ display:"flex", borderBottom:"1px solid var(--line)", flexShrink:0 }}>
        {tabs.map(t => (
          <button key={t.id} onClick={() => setTab(t.id)} style={{
            flex:1, padding:"5px 4px", border:"none", background:"none", cursor:"pointer",
            fontSize:12, fontWeight: tab===t.id?700:400,
            color: tab===t.id?"var(--blue)":"var(--ink-3)",
            borderBottom:`2px solid ${tab===t.id?"var(--blue)":"transparent"}`,
          }}>{t.label}</button>
        ))}
      </div>
      <div style={{ flex:1, overflow:"hidden" }}>
        {tab === "overview" && <AgentOverviewPanel agents={agents} />}
        {tab === "testbed"  && <MultiAgentTestbed sessionId={sessionId} />}
        {tab === "chat"     && <CommHistory agents={agents} />}
      </div>
    </div>
  );
}

// ── Reusable SVG Line Chart ───────────────────────────────────────────────────
function LineChart({ series, label, unit = "", color = "var(--blue)", W = 440, H = 80 }) {
  if (!series || series.length < 2) {
    return (
      <div style={{ width:W, height:H, display:"flex", alignItems:"center", justifyContent:"center",
        background:"var(--bg)", borderRadius:6, border:"1px solid var(--line)",
        color:"var(--ink-3)", fontSize:12 }}>
        {label}: no data yet
      </div>
    );
  }
  const pad = { t:8, r:6, b:22, l:36 };
  const iW = W - pad.l - pad.r;
  const iH = H - pad.t - pad.b;
  const minV = Math.min(...series), maxV = Math.max(...series);
  const rangeV = maxV - minV || 1;
  const toX = (i) => pad.l + (i / (series.length - 1)) * iW;
  const toY = (v) => pad.t + iH - ((v - minV) / rangeV) * iH;

  const pathD = series.map((v,i) => `${i===0?'M':'L'}${toX(i).toFixed(1)},${toY(v).toFixed(1)}`).join(' ');
  const areaD = `${pathD} L${toX(series.length-1).toFixed(1)},${pad.t+iH} L${pad.l},${pad.t+iH} Z`;

  // Tick values
  const ticks = [minV, (minV+maxV)/2, maxV].map(v => Math.round(v * 10) / 10);

  return (
    <svg width={W} height={H} style={{ display:"block", background:"var(--bg)", borderRadius:6, border:"1px solid var(--line)" }}>
      {/* Y grid + labels */}
      {ticks.map((v,i) => {
        const y = toY(v);
        return (
          <g key={i}>
            <line x1={pad.l} y1={y} x2={pad.l+iW} y2={y} stroke="var(--line)" strokeWidth={0.5} strokeDasharray="3,3" />
            <text x={pad.l-4} y={y+4} textAnchor="end" fontSize={10} fill="var(--ink-3)">{v}{unit}</text>
          </g>
        );
      })}
      {/* Area fill */}
      <path d={areaD} fill={color} opacity={0.12} />
      {/* Line */}
      <path d={pathD} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
      {/* Last value dot */}
      <circle cx={toX(series.length-1)} cy={toY(series[series.length-1])} r={3} fill={color} />
      {/* X label */}
      <text x={pad.l + iW/2} y={H-4} textAnchor="middle" fontSize={10} fill="var(--ink-3)" fontWeight={600}>{label}</text>
    </svg>
  );
}

// Multi-series line chart (one line per agent)
function MultiLineChart({ seriesMap, label, unit = "", W = 440, H = 90 }) {
  const entries = Object.entries(seriesMap).filter(([,s]) => s && s.length >= 2);
  if (entries.length === 0) {
    return (
      <div style={{ width:W, height:H, display:"flex", alignItems:"center", justifyContent:"center",
        background:"var(--bg)", borderRadius:6, border:"1px solid var(--line)",
        color:"var(--ink-3)", fontSize:12 }}>
        {label}: no data yet
      </div>
    );
  }
  const pad = { t:8, r:6, b:22, l:36 };
  const iW = W - pad.l - pad.r;
  const iH = H - pad.t - pad.b;
  const allVals = entries.flatMap(([,s]) => s);
  const minV = Math.min(...allVals), maxV = Math.max(...allVals);
  const rangeV = maxV - minV || 1;
  const maxLen = Math.max(...entries.map(([,s]) => s.length));
  const toX = (i, len) => pad.l + (i / (Math.max(len,2) - 1)) * iW;
  const toY = (v) => pad.t + iH - ((v - minV) / rangeV) * iH;
  const ticks = [minV, maxV].map(v => Math.round(v * 10) / 10);

  return (
    <svg width={W} height={H} style={{ display:"block", background:"var(--bg)", borderRadius:6, border:"1px solid var(--line)" }}>
      {ticks.map((v,i) => {
        const y = toY(v);
        return (
          <g key={i}>
            <line x1={pad.l} y1={y} x2={pad.l+iW} y2={y} stroke="var(--line)" strokeWidth={0.5} strokeDasharray="3,3" />
            <text x={pad.l-4} y={y+4} textAnchor="end" fontSize={10} fill="var(--ink-3)">{v}{unit}</text>
          </g>
        );
      })}
      {entries.map(([name, series], idx) => {
        const col = AGENT_COLORS[idx % AGENT_COLORS.length];
        const d = series.map((v,i) => `${i===0?'M':'L'}${toX(i,series.length).toFixed(1)},${toY(v).toFixed(1)}`).join(' ');
        return (
          <g key={name}>
            <path d={d} fill="none" stroke={col} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" opacity={0.9} />
            <circle cx={toX(series.length-1, series.length)} cy={toY(series[series.length-1])} r={3} fill={col} />
            <text x={toX(series.length-1, series.length)+5} y={toY(series[series.length-1])+4}
              fontSize={9} fill={col} fontWeight="bold">{name}</text>
          </g>
        );
      })}
      <text x={pad.l + iW/2} y={H-4} textAnchor="middle" fontSize={10} fill="var(--ink-3)" fontWeight={600}>{label}</text>
    </svg>
  );
}

// ── Agent Overview: aggregate stats + time-series charts from MetricsHub ──────
function AgentOverviewPanel({ agents }) {
  const pollData = usePoll();
  const sessions = pollData.sessions || [];
  const metrics  = useMetrics();
  const { series } = metrics;

  if (sessions.length === 0) return (
    <div style={{ display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center",
      height:"100%", gap:8, color:"var(--ink-3)", fontSize:14 }}>
      <div style={{ opacity:.3, display:"flex", justifyContent:"center", marginBottom:6 }}>{ICONS.robot(36)}</div>No agents in scene
    </div>
  );

  const totalCollisions = sessions.reduce((s,a) => s+(a.collisionCount||0), 0);
  const totalTurns      = sessions.reduce((s,a) => s+(a.totalTurns||0), 0);
  const running         = sessions.filter(s => s.status==="running").length;

  // Build multi-series data from MetricsHub
  const collisionSeries = {}, speedSeries = {}, turnsSeries = {};
  for (const [name, s] of Object.entries(series)) {
    if (s.collision?.length > 1) collisionSeries[name] = s.collision;
    if (s.speed?.length > 1)    speedSeries[name]    = s.speed;
    if (s.turns?.length > 1)    turnsSeries[name]    = s.turns;
  }

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", overflow:"auto",
      padding:"8px 10px", gap:8 }}>

      {/* Aggregate stat chips — large, one-glance readable */}
      <div style={{ display:"flex", gap:6, flexShrink:0 }}>
        {[
          { label:"Agents",       val:sessions.length, color:"var(--blue)" },
          { label:"Running",      val:running,          color:"#f59e0b" },
          { label:"Collisions",   val:totalCollisions,  color: totalCollisions>0?"#dc2626":"#16a34a" },
          { label:"Total Turns",  val:totalTurns,       color:"var(--ink-2)" },
        ].map(({label,val,color}) => (
          <div key={label} style={{ flex:1, textAlign:"center", padding:"8px 4px",
            background:"var(--panel)", borderRadius:8, border:"1px solid var(--line)" }}>
            <div style={{ fontSize:24, fontWeight:900, color, lineHeight:1 }}>{val}</div>
            <div style={{ fontSize:12, color:"var(--ink-3)", marginTop:3 }}>{label}</div>
          </div>
        ))}
      </div>

      {/* Time-series charts */}
      <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
        <MultiLineChart seriesMap={collisionSeries} label="Collisions over time" W={440} H={85} />
        <MultiLineChart seriesMap={speedSeries}     label="Speed (m/s) over time" unit="m/s" W={440} H={85} />
      </div>

      {Object.keys(series).length === 0 && (
        <div style={{ fontSize:12, color:"var(--ink-3)", textAlign:"center", padding:8 }}>
          Charts will appear after agents start moving (sampled every 5s)
        </div>
      )}
    </div>
  );
}

// ── Multi-Agent Testbed ───────────────────────────────────────────────────────
function MultiAgentTestbed({ sessionId }) {
  const [running, setRunning]   = useState(false);
  const [log, setLog]           = useState([]);
  const [count, setCount]       = useState(3);
  const [goal, setGoal]         = useState("Explore the area and report obstacles you encounter");

  const addLog = (msg) => setLog(prev => [...prev.slice(-30), `${new Date().toLocaleTimeString()} ${msg}`]);

  const spawnAndRun = async () => {
    setRunning(true);
    setLog([]);
    try {
      // Step 1: ensure PIE is active
      addLog("Starting PIE mode…");
      const pieStatus = await fetch(`${API_BASE}/pie-status`).then(r=>r.json());
      if (!pieStatus.active) {
        await fetch(`${API_BASE}/pie-start`, { method:"POST" });
        addLog("PIE start requested — waiting up to 30s…");
        // Poll until PIE is active (don't blindly wait fixed duration)
        let pieReady = false;
        for (let i = 0; i < 30; i++) {
          await new Promise(r => setTimeout(r, 1000));
          const check = await fetch(`${API_BASE}/pie-status`).then(r=>r.json()).catch(()=>({active:false}));
          if (check.active) { pieReady = true; break; }
        }
        if (!pieReady) { addLog("⚠ PIE did not start — agents may not work correctly"); }
        else { addLog("✓ PIE active"); }
      } else {
        addLog("PIE already active");
      }

      // Step 2: spawn agents
      addLog(`Spawning ${count} agents…`);
      const positions = Array.from({length: count}, (_,i) => {
        const angle = (2*Math.PI*i)/count;
        const r = 1500;
        return [Math.round(r*Math.cos(angle)), Math.round(r*Math.sin(angle)), 110];
      });
      const spawnMsg = positions.map((p,i) =>
        `spawn_agent(agent_name="TestAgent_${i+1}", agent_type="pedestrian", location=[${p.join(",")}])`
      ).join("\n");
      await fetch(`${API_BASE}/chat`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ message: spawnMsg, sessionId }),
      });
      addLog(`Spawned ${count} agents`);

      // Step 3: send goal to each agent
      for (let i = 1; i <= count; i++) {
        addLog(`Sending goal to TestAgent_${i}…`);
        await fetch(`${API_BASE}/agent-broadcast`, {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ text: goal, target: `TestAgent_${i}` }),
        });
        await new Promise(r => setTimeout(r, 400));
      }
      addLog("✓ All agents received goals. Testbed running.");
    } catch(e) {
      addLog(`Error: ${e.message}`);
    } finally {
      setRunning(false);
    }
  };

  const stopAll = async () => {
    setRunning(false);
    await fetch(`${API_BASE}/agent-stop-all`, { method:"POST" }).catch(()=>{});
    addLog("Stopped all agents");
  };

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", padding:8, gap:6, overflow:"auto" }}>
      <div style={{ fontSize:12, fontWeight:700, color:"var(--ink-2)" }}>Multi-Agent Testbed</div>
      <div style={{ display:"flex", gap:6, alignItems:"center" }}>
        <label style={{ fontSize:12, color:"var(--ink-3)" }}>Agents:</label>
        <select value={count} onChange={e=>setCount(Number(e.target.value))}
          style={{ fontSize:12, padding:"2px 4px", border:"1px solid var(--line)", borderRadius:4,
            background:"var(--panel)", color:"var(--ink-1)" }}>
          {[2,3,4,5].map(n => <option key={n} value={n}>{n}</option>)}
        </select>
      </div>
      <textarea value={goal} onChange={e=>setGoal(e.target.value)} rows={2}
        style={{ fontSize:12, padding:"4px 6px", border:"1px solid var(--line)", borderRadius:5,
          background:"var(--bg)", color:"var(--ink-1)", resize:"none", fontFamily:"inherit" }}
        placeholder="Navigation goal for all agents…" />
      <div style={{ display:"flex", gap:6 }}>
        <button onClick={spawnAndRun} disabled={running}
          style={{ flex:1, padding:"5px 8px", borderRadius:6, border:"none",
            background: running?"var(--line)":"var(--blue)", color:"#fff",
            cursor: running?"default":"pointer", fontSize:12, fontWeight:600 }}>
          {running ? "Running…" : "▶ Spawn & Run"}
        </button>
        <button onClick={stopAll}
          style={{ padding:"5px 10px", borderRadius:6, border:"1px solid var(--red)",
            background:"rgba(220,38,38,.1)", color:"#dc2626", cursor:"pointer", fontSize:12 }}>
          ■ Stop
        </button>
      </div>
      <div style={{ flex:1, overflow:"auto", background:"var(--bg)", borderRadius:6,
        border:"1px solid var(--line)", padding:4, fontSize:12, fontFamily:"monospace", color:"var(--ink-2)" }}>
        {log.map((l,i) => <div key={i}>{l}</div>)}
        {log.length === 0 && <div style={{ color:"var(--ink-3)" }}>Log will appear here…</div>}
      </div>
    </div>
  );
}

// ─── Communication History (group chat sidebar) ─────────────────────────────

function CommHistory({ agents }) {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState("");
  const [target, setTarget] = useState("all");
  const scrollRef = useRef(null);

  // Get chat log from unified poll
  const pollData = usePoll();
  useEffect(() => {
    const data = pollData.chatLog || [];
    if (data.length > 0) {
      setMessages(prev => {
        const existing = new Set(prev.map(m => `${m.from}-${m.timestamp}`));
        const newMsgs = data.filter(m => !existing.has(`${m.from}-${m.timestamp}`));
        if (!newMsgs.length) return prev;
        return [...prev, ...newMsgs].slice(-100);
      });
    }
  }, [pollData.chatLog]);

  useEffect(() => { if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight; }, [messages]);

  const handleSend = () => {
    const text = input.trim();
    if (!text) return;
    // Use broadcast endpoint — triggers agent turns automatically
    fetch(`${API_BASE}/agent-broadcast`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, target: target === "all" ? "all" : target }),
    }).catch(() => {});
    setMessages(prev => [...prev, { from: "user", to: target, text, timestamp: Date.now() }]);
    setInput("");
  };

  const colors = { user: "#0f172a" };
  (agents || []).forEach((a, i) => { colors[a.name] = AGENT_COLORS[i % AGENT_COLORS.length]; });

  // Render @mentions in text with color
  const renderText = (text) => {
    const parts = text.split(/(@\w+)/g);
    return parts.map((part, i) => {
      if (part.startsWith("@")) {
        const name = part.slice(1);
        return <span key={i} style={{ color: colors[name] || "var(--blue)", fontWeight: 600 }}>{part}</span>;
      }
      return part;
    });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", background: "var(--bg)" }}>
      <div style={{ padding: "10px 12px", borderBottom: "1px solid var(--line)", fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>
        Communication
      </div>
      <div ref={scrollRef} style={{ flex: 1, overflowY: "auto", padding: "8px 12px" }}>
        {messages.length === 0 ? (
          <div style={{ color: "var(--ink-2)", fontSize: 12, textAlign: "center", marginTop: 40 }}>
            Messages between you and agents will appear here.
          </div>
        ) : messages.map((m, i) => (
          <div key={i} style={{ marginBottom: 8, fontSize: 12, lineHeight: "1.5" }}>
            <div style={{ display: "flex", alignItems: "baseline", gap: 6 }}>
              <span style={{ fontWeight: 700, color: colors[m.from] || "#64748b" }}>{m.from === "user" ? "You" : m.from}</span>
              {m.to && m.to !== "all" && <span style={{ fontSize: 12, color: "var(--ink-3)" }}>to <span style={{ color: colors[m.to] || "var(--blue)" }}>@{m.to}</span></span>}
              <span style={{ fontSize: 12, color: "var(--ink-2)", marginLeft: "auto" }}>{new Date(m.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
            </div>
            <div style={{ color: "var(--ink)", marginTop: 2 }}>{renderText(m.text)}</div>
          </div>
        ))}
      </div>
      <div style={{ padding: "8px 12px", borderTop: "1px solid var(--line)", display: "flex", gap: 6 }}>
        <select value={target} onChange={e => setTarget(e.target.value)}
          style={{ background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 4, color: "var(--ink-3)", fontSize: 12, padding: "4px 6px" }}>
          <option value="all">@all</option>
          {(agents || []).map(a => <option key={a.name} value={a.name}>@{a.name}</option>)}
        </select>
        <input value={input} onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); handleSend(); } }}
          placeholder="Message..."
          style={{ flex: 1, background: "var(--panel)", border: "1px solid var(--line)", borderRadius: 4, padding: "5px 8px", color: "var(--ink)", fontSize: 12, outline: "none" }}
        />
        <button onClick={handleSend} disabled={!input.trim()} style={{
          background: input.trim() ? "#15803d" : "#e6e9ef", border: "none", borderRadius: 4,
          padding: "5px 10px", color: "#fff", fontSize: 12, cursor: input.trim() ? "pointer" : "default", opacity: input.trim() ? 1 : 0.5,
        }}>Send</button>
      </div>
    </div>
  );
}

// ─── AgentPanel — vertical split: agent cards top, comm bottom ───────────────
function AgentPanel({ sessionId, commHeight = 200, onCommHeightChange, hideComm = false }) {
  const agentsCtx = useAgents();
  const statusCtx = useStatus();
  const pieActive = statusCtx.pieActive;

  // Use fine-grained contexts for agents
  const contextAgents = useMemo(() => {
    const sessions = agentsCtx.sessions || [];
    if (sessions.length > 0) {
      return sessions.map(s => ({ name: s.agentName, cls: s.agentClass, location: s.location, status: s.status }));
    }
    return (agentsCtx.agents || []);
  }, [agentsCtx.sessions, agentsCtx.agents]);

  // Which agent is expanded to detail view
  const [expandedAgent, setExpandedAgent] = useState(null);

  // Close detail panel if the agent disappears from UE
  useEffect(() => {
    if (expandedAgent && !contextAgents.find(a => a.name === expandedAgent.name)) {
      setExpandedAgent(null);
    }
    // Also update expanded agent data if it changes (location, status)
    if (expandedAgent) {
      const updated = contextAgents.find(a => a.name === expandedAgent.name);
      if (updated && (updated.location !== expandedAgent.location || updated.status !== expandedAgent.status)) {
        setExpandedAgent(updated);
      }
    }
  }, [contextAgents, expandedAgent]);

  // Vertical resize handle for comm panel
  const resizeRef = useRef(null);
  const dragging   = useRef(false);
  const startY     = useRef(0);
  const startH     = useRef(commHeight);

  const onMouseDown = useCallback((e) => {
    e.preventDefault();
    dragging.current = true;
    startY.current   = e.clientY;
    startH.current   = commHeight;
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    if (resizeRef.current) resizeRef.current.classList.add("dragging");
    const onMove = (ev) => {
      if (!dragging.current) return;
      const delta = startY.current - ev.clientY; // drag up = taller comm
      const next  = Math.max(60, Math.min(400, startH.current + delta));
      onCommHeightChange?.(next);
    };
    const onUp = () => {
      dragging.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      if (resizeRef.current) resizeRef.current.classList.remove("dragging");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [commHeight, onCommHeightChange]);

  const empty = contextAgents.length === 0;

  return (
    <div className="sw-right-inner">
      {/* ── Agents pane (fills remaining height) ── */}
      <div className="sw-agents-pane" style={{ flex: 1 }}>
        {/* Sub-header with refresh button */}
        <div style={{ padding:"6px 10px", borderBottom:"1px solid var(--line)", display:"flex", alignItems:"center", gap:6, flexShrink:0, background:"var(--panel)" }}>
          <span style={{ fontSize:12, fontWeight:700, color:"var(--ink)" }}>
            Agents {contextAgents.length > 0 && <span style={{ color:"var(--ink-3)", fontWeight:400 }}>({contextAgents.length})</span>}
          </span>
          <div style={{ flex:1 }} />
          <button
            onClick={() => {
              fetch(`${API_BASE}/context-snapshot`, { method:"POST" }).catch(()=>{});
              fetch(`${API_BASE}/agent-discover`,   { method:"POST" }).catch(()=>{});
            }}
            title="Sync context + auto-discover player agents from UE"
            style={{ fontSize:12, padding:"3px 9px", borderRadius:5, border:"1px solid var(--line)",
              background:"none", cursor:"pointer", color:"var(--ink-3)", fontFamily:"inherit" }}>
            Discover
          </button>
          <span style={{ width:6, height:6, borderRadius:"50%", background:pieActive?"#22c55e":"#94a3b8", boxShadow: pieActive?"0 0 0 2px rgba(34,197,94,.2)":"none", flexShrink:0 }}/>
          <span style={{ fontSize:12, color:pieActive?"#16a34a":"var(--ink-3)" }}>
            {pieActive ? "PIE Active" : "No PIE"}
          </span>
        </div>

        {/* Floating detail window rendered at document.body level via portal */}
        {expandedAgent && ReactDOM.createPortal(
          <AgentDetailPanel
            agent={expandedAgent}
            sessionId={sessionId}
            pieActive={pieActive}
            colorIdx={contextAgents.findIndex(a => a.name === expandedAgent.name)}
            onClose={() => setExpandedAgent(null)}
          />,
          document.body
        )}

        {empty ? (
          <div style={{ flex:1, display:"flex", flexDirection:"column", alignItems:"center", justifyContent:"center", gap:8, padding:20 }}>
            <div style={{ opacity:0.3, display:"flex", justifyContent:"center", marginBottom:6 }}>{ICONS.robot(28)}</div>
            <div style={{ fontSize:12, color:"var(--ink-3)", textAlign:"center", lineHeight:1.5 }}>
              {pieActive ? "Spawn agents to see them here." : "Start PIE in Unreal Engine\nto enable agent control."}
            </div>
          </div>
        ) : (
          <div style={{ flex:1, overflowY:"auto", padding:8, display:"grid", gridTemplateColumns:"repeat(auto-fill, minmax(200px, 1fr))", gap:8, alignContent:"flex-start" }}>
            {contextAgents.map((a, i) => (
              <AgentCard key={a.name} agent={a} sessionId={sessionId} pieActive={pieActive} colorIdx={i}
                onExpand={setExpandedAgent}
              />
            ))}
          </div>
        )}
      </div>

      {/* ── Resize handle + comm pane — hidden when hideComm=true ── */}
      {!hideComm && (<>
        <div className="sw-resize-row" ref={resizeRef} onMouseDown={onMouseDown} title="Drag to resize" />
        <div className="sw-comm-pane" style={{ height: commHeight }}>
          <AgentAggregatePanelTabs agents={contextAgents} sessionId={sessionId} />
        </div>
      </>)}
    </div>
  );
}

// ─── Coding Agent Verifier Panel ─────────────────────────────────────────────
// Rule-based feedback (scene collisions) + LLM-based (VLM score) per coding turn

function CodingVerifierPanel({ sessionId, latestScreenshot }) {
  const [tab, setTab]         = useState("collisions");
  const [collData, setCollData] = useState(null);
  const [scores, setScores]   = useState([]);
  const [checking, setChecking] = useState(false);
  const [vlmRunning, setVlmRunning] = useState(false);
  const [vlmError, setVlmError] = useState(null);
  const metrics   = useMetrics();
  const sceneCollHistory = (metrics.sceneCollisions || []).map(c => c.count);

  const runCollisionCheck = useCallback(async () => {
    setChecking(true);
    try {
      // /api/scene-check runs UE Python AABB overlap + floating detection
      // Works in editor mode (no PIE needed), no UnrealCV vget required
      const data = await fetch(`${API_BASE}/scene-check`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({}),
      }).then(r => r.json());
      setCollData(data);
    } catch { setCollData(null); }
    finally { setChecking(false); }
  }, []);

  const runVlmScore = useCallback(async () => {
    setVlmRunning(true); setVlmError(null);
    try {
      const resp = await fetch(`${API_BASE}/screenshot/latest?t=${Date.now()}`);
      if (!resp.ok) throw new Error("No screenshot");
      const blob = await resp.blob();
      const base64 = await new Promise(resolve => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.readAsDataURL(blob);
      });
      // Send to VLM scoring endpoint
      const result = await fetch(`${API_BASE}/vlm-score`, {
        method:"POST", headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ imageDataUrl: base64, sessionId }),
      }).then(r => r.json());
      setScores(prev => [...prev.slice(-9), {
        ts: Date.now(), score: result.score, feedback: result.feedback,
        screenshot: base64, label: result.label,
      }]);
    } catch(e) { setVlmError(e.message); }
    finally { setVlmRunning(false); }
  }, [sessionId]);

  // Auto-trigger both checkers when scene screenshot changes
  // Defined AFTER runVlmScore to avoid TDZ in the dependency array
  const prevScreenRef = useRef(null);
  useEffect(() => {
    if (!latestScreenshot || latestScreenshot === prevScreenRef.current) return;
    prevScreenRef.current = latestScreenshot;
    runCollisionCheck();
    runVlmScore();
  }, [latestScreenshot, runCollisionCheck, runVlmScore]);

  const tabs = [
    { id:"collisions", label:"Rule-based Checker" },
    { id:"vlm",        label:"VLM Score" },
  ];

  const collCount  = collData?.collision_count ?? "—";
  const floatCount = collData?.floating_count  ?? "—";
  const collColor  = typeof collCount  === "number" ? (collCount  === 0 ? "#16a34a" : "#dc2626") : "var(--ink-3)";
  const floatColor = typeof floatCount === "number" ? (floatCount === 0 ? "#16a34a" : "#f59e0b") : "var(--ink-3)";

  const FS = "var(--fs-body)"; // 13px minimum throughout

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", background:"var(--bg)" }}>
      {/* Tab bar + action button */}
      <div style={{ display:"flex", alignItems:"center", padding:"6px 10px",
        borderBottom:"1px solid var(--line)", flexShrink:0, gap:6 }}>
        <div style={{ display:"flex", gap:4, flex:1 }}>
          {tabs.map(t => (
            <button key={t.id} onClick={() => setTab(t.id)} style={{
              padding:"5px 12px", fontSize:FS, borderRadius:6, border:"none", cursor:"pointer",
              background: tab===t.id?"var(--orange)":"var(--bg)",
              color: tab===t.id?"#fff":"var(--ink-2)",
              fontWeight: tab===t.id?700:500, fontFamily:"inherit",
            }}>{t.label}</button>
          ))}
        </div>
        {tab === "collisions" && (
          <button onClick={runCollisionCheck} disabled={checking}
            style={{ fontSize:FS, padding:"5px 12px", borderRadius:6,
              border:"1px solid var(--line)", background:"var(--panel)",
              cursor:"pointer", color:"var(--ink-2)", fontFamily:"inherit" }}>
            {checking ? "Checking…" : "↻ Check"}
          </button>
        )}
        {tab === "vlm" && (
          <button onClick={runVlmScore} disabled={vlmRunning}
            style={{ fontSize:FS, padding:"5px 12px", borderRadius:6,
              border:"1px solid var(--line)", background:"var(--panel)",
              cursor:"pointer", color:"var(--ink-2)", fontFamily:"inherit" }}>
            {vlmRunning ? "Scoring…" : "↻ Score"}
          </button>
        )}
      </div>

      {/* Body */}
      <div style={{ flex:1, overflow:"auto", padding:8 }}>
        {tab === "collisions" && (
          collData ? (
            <div>
              {/* Stat chips */}
              <div style={{ display:"flex", gap:5, marginBottom:10 }}>
                {[
                  { val:collData.collision_count ?? 0,      label:"Collisions", color:collColor },
                  { val:collData.floating_count  ?? 0,      label:"Floating",   color:floatColor },
                  { val:collData.checked_actors_count ?? 0, label:"Actors",     color:"var(--ink-2)" },
                ].map(({val,label,color}) => (
                  <div key={label} style={{ flex:1, textAlign:"center", padding:"8px 4px",
                    background:"var(--panel)", borderRadius:8, border:"1px solid var(--line)" }}>
                    <div style={{ fontSize:24, fontWeight:900, color, lineHeight:1 }}>{val}</div>
                    <div style={{ fontSize:11, color:"var(--ink-3)", marginTop:3 }}>{label}</div>
                  </div>
                ))}
              </div>
              {/* Collision history chart */}
              {sceneCollHistory.length > 1 && (
                <div style={{ marginBottom:10 }}>
                  <LineChart series={sceneCollHistory} label="Collision history" color="#dc2626" W={380} H={72} />
                </div>
              )}
              {/* Collision pairs */}
              {(collData.collision_pairs||[]).slice(0,5).map((p,i) => (
                <div key={i} style={{ fontSize:FS, padding:"6px 8px",
                  background:"rgba(220,38,38,.06)", borderRadius:6, marginBottom:4,
                  borderLeft:"3px solid #dc2626", color:"var(--ink-2)", lineHeight:1.4 }}>
                  <strong>{p.actor1}</strong> ↔ <strong>{p.actor2}</strong>
                  <span style={{ color:"var(--ink-3)", marginLeft:6, fontSize:12 }}>
                    {p.collision_type} · {Math.round(p.penetration_depth)}cm
                  </span>
                </div>
              ))}
              {collData.collision_count === 0 && (
                <div style={{ fontSize:FS, color:"#16a34a", textAlign:"center", padding:"8px 0 4px", fontWeight:600 }}>
                  No collisions detected
                </div>
              )}
              {/* Floating actors */}
              {(collData.floating_actors||[]).length > 0 && (
                <div style={{ marginTop:8 }}>
                  <div style={{ fontSize:11, fontWeight:600, color:"var(--ink-3)", marginBottom:4 }}>
                    FLOATING ACTORS
                  </div>
                  {(collData.floating_actors||[]).slice(0,5).map((f,i) => (
                    <div key={i} style={{ fontSize:FS, padding:"5px 8px",
                      background: f.no_surface ? "rgba(239,68,68,.08)" : "rgba(245,158,11,.08)",
                      borderRadius:6, marginBottom:3,
                      borderLeft: `3px solid ${f.no_surface ? "#ef4444" : "#f59e0b"}`,
                      color:"var(--ink-2)" }}>
                      <strong>{f.name}</strong>
                      <span style={{ color:"var(--ink-3)", marginLeft:6, fontSize:12 }}>
                        {f.no_surface
                          ? "no surface below"
                          : `+${Math.round(f.gap_cm)}cm above surface (Z=${f.surface_z})`}
                      </span>
                    </div>
                  ))}
                </div>
              )}
              {collData.floating_count === 0 && collData.collision_count === 0 && (
                <div style={{ fontSize:FS, color:"var(--ink-3)", textAlign:"center", paddingBottom:8 }}>
                  All actors grounded · no overlaps
                </div>
              )}
            </div>
          ) : (
            <div style={{ fontSize:FS, color:"var(--ink-3)", textAlign:"center", padding:16 }}>
              {checking ? "Checking…" : "Click ↻ Check to run collision detection"}
            </div>
          )
        )}
        {tab === "vlm" && (
          <div>
            {vlmError && <div style={{ fontSize:FS, color:"#dc2626", marginBottom:6 }}>{vlmError}</div>}
            {scores.length === 0 && !vlmRunning && (
              <div style={{ fontSize:FS, color:"var(--ink-3)", textAlign:"center", padding:16 }}>
                Click ↻ Score to evaluate the current scene with VLM
              </div>
            )}
            {scores.slice().reverse().map((s,i) => (
              <div key={i} style={{ marginBottom:8, padding:8, background:"var(--panel)",
                borderRadius:8, border:"1px solid var(--line)" }}>
                <div style={{ display:"flex", alignItems:"center", gap:8, marginBottom:4 }}>
                  <span style={{ fontSize:20, fontWeight:800,
                    color: s.score >= 7?"#16a34a":s.score >= 4?"#f59e0b":"#dc2626" }}>
                    {s.score}<span style={{ fontSize:13, fontWeight:500, color:"var(--ink-3)" }}>/10</span>
                  </span>
                  {s.label && <span style={{ fontSize:12, background:"var(--bg)", borderRadius:4,
                    padding:"2px 7px", color:"var(--ink-2)", fontWeight:600 }}>{s.label}</span>}
                  <span style={{ fontSize:12, color:"var(--ink-3)", marginLeft:"auto" }}>
                    {new Date(s.ts).toLocaleTimeString()}
                  </span>
                </div>
                {s.feedback && <div style={{ fontSize:FS, color:"var(--ink-2)", lineHeight:1.5 }}>{s.feedback}</div>}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── ViewportPanel ───────────────────────────────────────────────────────────

function ViewportPanel({ latestScreenshot }) {
  const [mode, setMode]           = useState("pixelstream"); // default to live stream
  const [imgKey, setImgKey]       = useState(0);
  const [screenshotUrl, setScreenshotUrl] = useState(null);
  const [autoRefresh, setAutoRefresh]     = useState(false);
  const [refreshInterval, setRefreshInterval] = useState(5);
  const [cameraMoving, setCameraMoving]   = useState(false);
  const intervalRef = useRef(null);

  const handleCameraPreset = useCallback(async (args) => {
    setCameraMoving(true);
    try { await sendCameraCommand("set_camera", args); }
    finally { setCameraMoving(false); }
  }, []);

  const [playerUrl, setPlayerUrl] = useState(null);
  useEffect(() => {
    fetch("/api/pixel-streaming-url")
      .then(r => r.json())
      .then(d => {
        if (d.url) {
          const externalCirrusUrl = (() => {
            try {
              return new URLSearchParams(window.location.search).get("cirrusUrl") || "";
            } catch {
              return "";
            }
          })();
          // Load our custom ue-player.html (not the default Cirrus player.html)
          // Pass a public Cirrus tunnel URL in Colab, or the local Cirrus port otherwise.
          const cirrusPort = d.detectedPort || (() => {
            try { return new URL(d.url).port || 8685; } catch { return 8685; }
          })();
          // All PS settings passed as URL params — player.js reads them via useUrlParams:true
          const ps = new URLSearchParams({
            StreamerId:             'Editor',   // subscribe directly, skip streamer-select UI
            StreamerAutoJoinInterval: '3',      // retry every 3s when no streamer yet
            MaxReconnectAttempts:     '0',      // unlimited retries
            AutoConnect:      'true',
            AutoPlayVideo:    'true',
            StartVideoMuted:  'true',
            WaitForStreamer:  'true',
            HoveringMouse:    'true',
            KeyboardInput:    'true',
            MouseInput:       'true',
            GamepadInput:     'true',
            TouchInput:       'true',
            ControlsQuality:  'true',
            MatchViewportRes: 'true',
            TimeoutIfIdle:    'false',
            WebRTCFPS:        '60',
          });
          if (externalCirrusUrl) ps.set('cirrusUrl', externalCirrusUrl);
          else ps.set('cirrus', String(cirrusPort));
          setPlayerUrl(`/ue-player.html?${ps.toString()}`);
        }
      })
      .catch(() => {});
  }, []);

  // Update from prop — fetch as blob for reliable display
  useEffect(() => {
    if (!latestScreenshot) return;
    setMode("screenshot");
    let cancelled = false;
    const sep = latestScreenshot.includes("?") ? "&" : "?";
    const url = latestScreenshot + `${sep}t=${Date.now()}`;
    fetch(url)
      .then((r) => (r.ok ? r.blob() : Promise.reject()))
      .then((blob) => {
        if (!cancelled) {
          setScreenshotUrl(URL.createObjectURL(blob));
          setImgKey((k) => k + 1);
        }
      })
      .catch(() => {
        // Fallback to direct URL if blob fetch fails
        if (!cancelled) {
          setScreenshotUrl(url);
          setImgKey((k) => k + 1);
        }
      });
    return () => { cancelled = true; };
  }, [latestScreenshot]);

  // Initial fetch
  useEffect(() => {
    fetchLatestScreenshot();
  }, []);

  // Auto-refresh — only run when tab is visible AND mode=screenshot
  // Uses visibilitychange to pause when user switches away (reduces background load)
  useEffect(() => {
    if (intervalRef.current) clearInterval(intervalRef.current);
    if (!autoRefresh || mode !== "screenshot") return;

    const tick = () => {
      if (document.visibilityState === "visible") fetchLatestScreenshot();
    };
    intervalRef.current = setInterval(tick, refreshInterval * 1000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
      document.removeEventListener("visibilitychange", tick);
    };
  }, [autoRefresh, mode, refreshInterval]);

  const fetchLatestScreenshot = async () => {
    try {
      const resp = await fetch(`/api/screenshot/latest?t=${Date.now()}`);
      if (resp.ok) {
        const blob = await resp.blob();
        setScreenshotUrl(URL.createObjectURL(blob));
        setImgKey((k) => k + 1);
      }
    } catch {}
  };

  return (
    <div style={{ display:"flex", flexDirection:"column", height:"100%", background:"#0b1220" }}>

      {/* Dark toolbar */}
      <div style={{
        padding:"7px 12px",
        borderBottom:"1px solid rgba(255,255,255,.07)",
        display:"flex", alignItems:"center", gap:6,
        flexShrink:0, background:"#0d1526", userSelect:"none",
      }}>
        {/* Mode toggle */}
        <div style={{ display:"flex", gap:3 }}>
          {[
            { id:"pixelstream", label:"Live" },
            { id:"screenshot",  label:"Shot" },
          ].map(m => (
            <button key={m.id} onClick={() => setMode(m.id)} style={{
              padding:"3px 10px", fontSize:12, borderRadius:6, cursor:"pointer",
              border: `1px solid ${mode===m.id?"var(--blue)":"rgba(255,255,255,.1)"}`,
              background: mode===m.id?"rgba(37,99,235,.25)":"transparent",
              color: mode===m.id?"#93c5fd":"#94a3b8",
              fontWeight: mode===m.id?700:400,
            }}>{m.label}</button>
          ))}
        </div>

        {/* Screenshot controls */}
        {mode === "screenshot" && (
          <>
            <button onClick={fetchLatestScreenshot} style={{
              padding:"3px 8px", fontSize:12, borderRadius:5, cursor:"pointer",
              border:"1px solid rgba(255,255,255,.15)", background:"rgba(255,255,255,.08)",
              color:"#e2e8f0",
            }}>↻ Refresh</button>
            <label style={{ display:"flex", alignItems:"center", gap:3, fontSize:12, color:"#64748b", cursor:"pointer" }}>
              <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} style={{ accentColor:"var(--blue)" }}/>
              Auto
            </label>
            <select value={refreshInterval} onChange={e => setRefreshInterval(Number(e.target.value))} style={{
              padding:"2px 5px", fontSize:12, background:"rgba(255,255,255,.08)",
              border:"1px solid rgba(255,255,255,.1)", borderRadius:4, color:"#94a3b8",
            }}>
              {[2,3,5,10].map(s => <option key={s} value={s}>{s}s</option>)}
            </select>
          </>
        )}

        {/* Camera presets */}
        <div style={{ display:"flex", gap:2, alignItems:"center", marginLeft:4 }}>
          <span style={{ fontSize:12, color:"#475569", letterSpacing:".06em" }}>CAM</span>
          {CAMERA_PRESETS.map(p => (
            <button key={p.label} title={p.title} disabled={cameraMoving}
              onClick={() => handleCameraPreset(p.args)} style={{
                padding:"2px 7px", fontSize:12, borderRadius:4, cursor:cameraMoving?"wait":"pointer",
                border:"1px solid rgba(255,255,255,.1)", background:"rgba(255,255,255,.06)",
                color: cameraMoving?"#334155":"#94a3b8",
              }}>{p.label}</button>
          ))}
          <button title="Unlock camera from agent" disabled={cameraMoving}
            onClick={() => { setCameraMoving(true); sendCameraCommand("unpilot_camera").finally(()=>setCameraMoving(false)); }}
            style={{
              padding:"2px 7px", fontSize:12, borderRadius:4, cursor:cameraMoving?"wait":"pointer",
              border:"1px solid rgba(220,38,38,.3)", background:"rgba(220,38,38,.1)",
              color: cameraMoving?"#334155":"#fca5a5",
            }}>✕ Unlock</button>
        </div>

        <div style={{ flex:1 }}/>

        {/* Open in tab */}
        {playerUrl && (
          <a href={playerUrl} target="_blank" rel="noreferrer" style={{
            padding:"3px 8px", fontSize:12, borderRadius:5,
            border:"1px solid rgba(255,255,255,.1)", background:"rgba(255,255,255,.06)",
            color:"#64748b", textDecoration:"none", display:"inline-block",
          }}>⧉ Pop out</a>
        )}
      </div>

      {/* Viewport — live stream always mounted, screenshot overlays */}
      <div style={{ flex:1, position:"relative", overflow:"hidden" }}>
        {/* PixelStreamPlayer — always mounted (preserves WebRTC) */}
        <div style={{ width:"100%", height:"100%", display: mode==="pixelstream"?"block":"none" }}>
          <PixelStreamPlayer playerUrl={playerUrl} />
        </div>
        {mode === "screenshot" && (
          <ScreenshotView src={screenshotUrl} imgKey={imgKey} onRefresh={fetchLatestScreenshot} />
        )}
      </div>

      {/* Micro status bar */}
      <div style={{
        padding:"2px 12px", background:"#0d1526",
        borderTop:"1px solid rgba(255,255,255,.05)",
        display:"flex", alignItems:"center", gap:14,
        fontSize:12, color:"#334155", flexShrink:0,
      }}>
        <span>UE 5.3.2 · SimWorld Studio</span>
        {playerUrl && <span>{playerUrl.match(/cirrus=(\d+)/)?.[1] ? `Cirrus :${playerUrl.match(/cirrus=(\d+)/)[1]}` : playerUrl}</span>}
        {mode==="screenshot" && screenshotUrl && <span style={{ color:"var(--green)", marginLeft:"auto", display:"inline-flex", alignItems:"center", gap:4 }}>{ICONS.check(12)} Screenshot ready</span>}
      </div>
    </div>
  );
}

// ─── AssetPlaceholder ────────────────────────────────────────────────────────

function AssetPlaceholder({ id, category }) {
  const color = CATEGORY_COLORS[category] || "var(--line)";
  const numMatch = id.match(/(\d+)/);
  if (numMatch) parseInt(numMatch[1]);

  return (
    <div
      style={{
        width: "100%",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        justifyContent: "center",
        background: `linear-gradient(135deg, ${color}22 0%, ${color}11 100%)`,
        gap: 4,
      }}
    >
      <span style={{ fontSize: 28, opacity: 0.6, display: "inline-flex" }}>{(CATEGORY_ICONS[category] || ICONS.cube)(28)}</span>
      <span
        style={{
          fontSize: 12,
          color,
          opacity: 0.8,
          fontWeight: 600,
          maxWidth: "90%",
          textAlign: "center",
          wordBreak: "break-all",
        }}
      >
        {id.replace("BP_", "").replace(/_/g, " ")}
      </span>
    </div>
  );
}

// ─── AssetCard (grid view) ───────────────────────────────────────────────────

function AssetCard({ item, category, onInsert }) {
  const [imgError, setImgError] = useState(false);
  const thumbnailUrl = `/thumbnails/${item.id}.png`;

  return (
    <div
      onClick={() => onInsert(item.id)}
      title={item.path || item.id}
      style={{
        borderRadius: 6,
        overflow: "hidden",
        border: "1px solid var(--line)",
        background: "var(--panel)",
        cursor: "pointer",
        transition: "border-color 0.15s",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.borderColor = "var(--blue)")}
      onMouseLeave={(e) => (e.currentTarget.style.borderColor = "var(--line)")}
    >
      <div
        style={{
          height: 90,
          background: "var(--bg)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          overflow: "hidden",
        }}
      >
        {imgError ? (
          <AssetPlaceholder id={item.id} category={category} />
        ) : (
          <img
            src={thumbnailUrl}
            alt={item.id}
            onError={() => setImgError(true)}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        )}
      </div>
      <div style={{ padding: "5px 8px" }}>
        <div
          style={{
            fontSize: 12,
            fontWeight: 500,
            color: "var(--ink)",
            wordBreak: "break-all",
            lineHeight: 1.3,
          }}
        >
          {item.id}
        </div>
      </div>
    </div>
  );
}

// ─── AssetListItem ───────────────────────────────────────────────────────────

function AssetListItem({ item, category, onInsert }) {
  const [imgError, setImgError] = useState(false);
  const thumbnailUrl = `/thumbnails/${item.id}.png`;

  return (
    <div
      onClick={() => onInsert(item.id)}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "4px 8px",
        borderRadius: 4,
        cursor: "pointer",
      }}
      onMouseEnter={(e) => (e.currentTarget.style.background = "#ffffff")}
      onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
    >
      <div
        style={{
          width: 36,
          height: 36,
          borderRadius: 4,
          overflow: "hidden",
          background: "var(--bg)",
          flexShrink: 0,
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          border: "1px solid var(--line)",
        }}
      >
        {imgError ? (
          <span style={{ fontSize: 16, display: "inline-flex" }}>{(CATEGORY_ICONS[category] || ICONS.cube)(16)}</span>
        ) : (
          <img
            src={thumbnailUrl}
            alt={item.id}
            onError={() => setImgError(true)}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        )}
      </div>
      <div style={{ flex: 1, minWidth: 0 }}>
        <div style={{ fontSize: 12, color: "var(--ink)", fontWeight: 500 }}>{item.id}</div>
        {item.path && (
          <div
            style={{
              fontSize: 12,
              color: "var(--ink-2)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {item.path.split("/").pop()?.replace("_C", "")}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── AssetBrowser (lazy one-level-at-a-time, UE only) ────────────────────────

function AssetBrowser({ onInsert }) {
  const [browsePath, setBrowsePath] = useState("/Game/");
  const [search, setSearch]         = useState("");
  const [page,   setPage]           = useState(0);
  // dirCache: Map<path, { loading, loaded, dirs, assets, source }>
  const [dirCache, setDirCache]     = useState(() => new Map());
  const containerRef = useRef(null);
  const PAGE_SIZE    = 40;

  // Ref that always holds the latest dirCache — safe to read synchronously in callbacks
  const dirCacheRef = useRef(new Map());
  dirCacheRef.current = dirCache;  // updated every render, no stale reads

  // Tracks in-flight fetches to prevent duplicates
  const pendingRef = useRef(new Set());

  // fetchDir: no side-effects inside setState, state read via ref
  const fetchDir = useCallback((path) => {
    const entry = dirCacheRef.current.get(path);
    if (entry?.loading || entry?.loaded || pendingRef.current.has(path)) return;

    pendingRef.current.add(path);
    setDirCache(prev => {
      const next = new Map(prev);
      next.set(path, { loading: true, loaded: false, dirs: [], assets: [], source: null });
      return next;
    });

    fetch(`${API_BASE}/asset-ls?path=${encodeURIComponent(path)}`)
      .then(r => r.json())
      .then(data => setDirCache(p => {
        const m = new Map(p);
        m.set(path, { loading: false, loaded: true,
          dirs: data.dirs || [], assets: data.assets || [], source: data.source });
        return m;
      }))
      .catch(() => setDirCache(p => {
        const m = new Map(p);
        m.set(path, { loading: false, loaded: true, dirs: [], assets: [], source: 'error' });
        return m;
      }))
      .finally(() => pendingRef.current.delete(path));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Load root the first time the drawer becomes visible
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const obs = new IntersectionObserver(([e]) => {
      if (e.isIntersecting) { fetchDir("/Game/"); obs.disconnect(); }
    }, { threshold: 0.1 });
    obs.observe(el);
    return () => obs.disconnect();
  }, [fetchDir]);

  // Load a directory whenever browsePath changes
  useEffect(() => { fetchDir(browsePath); }, [browsePath, fetchDir]);

  // Auto-retry every 3 s when UE is not reachable
  useEffect(() => {
    const cur = dirCache.get(browsePath);
    if (cur?.source !== 'unavailable' && cur?.source !== 'error') return;
    const t = setTimeout(() => {
      // Clear cached result so fetchDir will re-run
      pendingRef.current.delete(browsePath);
      setDirCache(p => { const m = new Map(p); m.delete(browsePath); return m; });
    }, 3000);
    return () => clearTimeout(t);
  }, [dirCache, browsePath]);

  const navigate = (path) => { setBrowsePath(path); setSearch(""); setPage(0); };
  const navigateUp = () => {
    const parts = browsePath.replace(/\/$/, "").split("/").filter(Boolean);
    if (parts.length <= 1) return navigate("/Game/");
    parts.pop();
    navigate("/" + parts.join("/") + "/");
  };

  const [loadingMap, setLoadingMap] = useState(null);  // fullPath being loaded

  const handleDblClick = (a) => {
    if (a.type === 'map') {
      // Tell UE to open the map in-editor
      setLoadingMap(a.fullPath);
      fetch(`${API_BASE}/load-map`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: a.fullPath }),
      })
        .then(r => r.json())
        .then(d => { if (d.error) console.warn('load-map:', d.error); })
        .catch(() => {})
        .finally(() => setLoadingMap(null));
    } else if (a.type === 'blueprint' || a.type === 'static_mesh') {
      // Insert into Coding Agent context
      onInsert?.(a.fullPath);
    }
    // other types: no-op
  };

  const retry = () => {
    pendingRef.current.delete(browsePath);
    setDirCache(prev => { const m = new Map(prev); m.delete(browsePath); return m; });
  };

  const cur = dirCache.get(browsePath) || { loading: false, loaded: false, dirs: [], assets: [], source: null };
  const { loading, loaded, dirs, assets, source } = cur;

  const q = search.toLowerCase();
  const filteredAssets = q ? assets.filter(a => a.name.toLowerCase().includes(q)) : assets;
  const pageAssets = filteredAssets.slice(0, (page + 1) * PAGE_SIZE);
  const hasMore    = pageAssets.length < filteredAssets.length;
  const breadcrumbs = browsePath.replace(/\/$/, "").split("/").filter(Boolean);

  // ── States: unloaded / loading / unavailable / ready ──
  if (!loaded && !loading) {
    return (
      <div ref={containerRef} style={{ flex:1, display:"flex", flexDirection:"column",
        alignItems:"center", justifyContent:"center", gap:8,
        color:"var(--ink-3)", fontSize:"var(--fs-body)" }}>
        <div style={{ opacity:.3 }}>{ICONS.cube(24)}</div>
        <div>Assets</div>
      </div>
    );
  }

  if (loading) {
    return (
      <div ref={containerRef} style={{ flex:1, display:"flex", flexDirection:"column",
        alignItems:"center", justifyContent:"center", gap:8,
        color:"var(--ink-3)", fontSize:"var(--fs-body)" }}>
        <div style={{ opacity:.4 }}>{ICONS.cube(24)}</div>
        <div>Loading…</div>
      </div>
    );
  }

  if (source === 'unavailable' || source === 'error') {
    return (
      <div ref={containerRef} style={{ flex:1, display:"flex", flexDirection:"column",
        alignItems:"center", justifyContent:"center", gap:8,
        color:"var(--ink-3)", fontSize:"var(--fs-body)" }}>
        <div style={{ opacity:.3 }}>{ICONS.cube(24)}</div>
        <div style={{ fontWeight:600 }}>UE not running</div>
        <div style={{ fontSize:12 }}>Start Unreal Engine to browse assets</div>
        <button onClick={retry}
          style={{ marginTop:4, padding:"4px 14px", borderRadius:6, border:"1px solid var(--line)",
            background:"var(--panel)", cursor:"pointer", fontSize:"var(--fs-body)",
            color:"var(--blue)", fontFamily:"inherit" }}>
          Retry
        </button>
      </div>
    );
  }

  return (
    <div ref={containerRef} style={{ display:"flex", flexDirection:"column", height:"100%",
      background:"var(--bg)", overflow:"hidden" }}>

      {/* Toolbar */}
      <div style={{ flexShrink:0, padding:"5px 8px", borderBottom:"1px solid var(--line)",
        display:"flex", alignItems:"center", gap:4 }}>
        {browsePath !== "/Game/" && (
          <button onClick={navigateUp} style={{ fontSize:"var(--fs-body)", padding:"3px 8px",
            borderRadius:5, border:"1px solid var(--line)", background:"none",
            cursor:"pointer", color:"var(--ink-2)", fontFamily:"inherit" }}>↑</button>
        )}
        <div style={{ display:"flex", alignItems:"center", gap:2, fontSize:"var(--fs-body)",
          color:"var(--ink-3)", flex:1, overflow:"hidden" }}>
          <span style={{ cursor:"pointer", color:"var(--blue)" }} onClick={() => navigate("/Game/")}>Game</span>
          {breadcrumbs.filter(s => s !== "Game").map((seg, i, arr) => (
            <span key={i} style={{ display:"flex", alignItems:"center", gap:2 }}>
              <span>/</span>
              <span style={{ cursor:"pointer", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap",
                color: i === arr.length - 1 ? "var(--ink-1)" : "var(--blue)" }}
                onClick={() => {
                  const segs = ["Game", ...arr.slice(0, i + 1)];
                  navigate("/" + segs.join("/") + "/");
                }}>{seg}</span>
            </span>
          ))}
        </div>
        <input value={search} onChange={e => { setSearch(e.target.value); setPage(0); }}
          placeholder="Filter…"
          style={{ width:100, padding:"4px 8px", fontSize:"var(--fs-body)",
            border:"1px solid var(--line)", borderRadius:5,
            background:"var(--panel)", color:"var(--ink-1)", outline:"none" }} />
      </div>

      {/* Body */}
      <div style={{ flex:1, overflow:"auto", padding:6 }}>

        {/* Folder tiles */}
        {!search && dirs.length > 0 && (
          <div style={{ display:"flex", flexWrap:"wrap", gap:5, marginBottom:8 }}>
            {dirs.map(d => (
              <button key={d.path} onClick={() => navigate(d.path)}
                style={{ display:"flex", alignItems:"center", gap:5, padding:"5px 10px",
                  border:"1px solid var(--line)", borderRadius:7, background:"var(--panel)",
                  cursor:"pointer", fontSize:"var(--fs-body)", color:"var(--ink-2)",
                  fontFamily:"inherit", fontWeight:500 }}>
                {ICONS.folder(13)} {d.name}
              </button>
            ))}
          </div>
        )}

        {/* Asset tiles */}
        {pageAssets.length > 0 && (
          <div style={{ display:"grid", gridTemplateColumns:"repeat(auto-fill,minmax(110px,1fr))", gap:5 }}>
            {pageAssets.map((a, i) => {
              const isActionable = a.type === 'map' || a.type === 'blueprint' || a.type === 'static_mesh';
              const isLoading    = loadingMap === a.fullPath;
              return (
                <div key={`${a.fullPath}-${i}`}
                  title={isActionable
                    ? (a.type === 'map' ? 'Double-click to open map in UE' : 'Double-click to add to context')
                    : a.fullPath}
                  onDoubleClick={() => handleDblClick(a)}
                  style={{ padding:"8px 6px", borderRadius:8, border:"1px solid var(--line)",
                    background: isLoading ? "var(--blue-soft)" : "var(--panel)",
                    cursor: isActionable ? "pointer" : "default", textAlign:"center",
                    fontSize:"var(--fs-body)", color:"var(--ink-2)",
                    transition:"border-color .12s, box-shadow .12s, background .12s", userSelect:"none",
                    opacity: isActionable ? 1 : 0.6 }}
                  onMouseEnter={e => { if (isActionable) { e.currentTarget.style.borderColor="var(--blue)"; e.currentTarget.style.boxShadow="0 0 0 2px var(--blue-soft)"; } }}
                  onMouseLeave={e => { e.currentTarget.style.borderColor="var(--line)"; e.currentTarget.style.boxShadow="none"; }}>
                  <div style={{ display:"flex", justifyContent:"center", marginBottom:4, color:"var(--ink-3)" }}>
                    {isLoading ? ICONS.refresh(22)
                      : a.type === 'blueprint' ? ICONS.building(22)
                      : a.type === 'map' ? ICONS.map(22)
                      : ICONS.cube(22)}
                  </div>
                  <div style={{ overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap",
                    fontWeight:600, fontSize:12 }}>
                    {a.name.replace(/^BP_/, "").replace(/_/g, " ")}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        {pageAssets.length === 0 && dirs.length === 0 && (
          <div style={{ textAlign:"center", padding:24, color:"var(--ink-3)", fontSize:"var(--fs-body)" }}>
            {search ? `No results for "${search}"` : "Empty folder"}
          </div>
        )}

        {hasMore && (
          <div style={{ textAlign:"center", marginTop:8 }}>
            <button onClick={() => setPage(p => p + 1)}
              style={{ padding:"6px 18px", borderRadius:7, border:"1px solid var(--line)",
                background:"var(--panel)", cursor:"pointer",
                fontSize:"var(--fs-body)", color:"var(--blue)", fontFamily:"inherit" }}>
              Load more ({filteredAssets.length - pageAssets.length} remaining)
            </button>
          </div>
        )}
      </div>

      {/* Footer */}
      <div style={{ flexShrink:0, padding:"4px 10px", borderTop:"1px solid var(--line)",
        fontSize:12, color:"var(--ink-3)", display:"flex", justifyContent:"space-between" }}>
        <span>
          {!search && dirs.length > 0 && `${dirs.length} folders · `}
          {pageAssets.length}/{filteredAssets.length} assets{search && " (filtered)"}
        </span>
        <span style={{ display:"flex", alignItems:"center", gap:4 }}>
          <span style={{ width:6, height:6, borderRadius:"50%", background:"#16a34a", display:"inline-block" }} />
          Live UE
        </span>
      </div>
    </div>
  );
}

// ─── SceneManager ────────────────────────────────────────────────────────────

function SceneManager({ onLoadScene, currentSessionId }) {
  const [scenes, setScenes] = useState([]);
  const [loading, setLoading] = useState(false); // start false — lazy load
  const containerRef = useRef(null);
  const loadedRef    = useRef(false);

  const reload = useCallback(() => {
    setLoading(true);
    fetchScenes()
      .then(setScenes)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  // Lazy: only fetch when component scrolls into view (drawer opened)
  useEffect(() => {
    if (loadedRef.current) return;
    const el = containerRef.current;
    if (!el) return;
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting && !loadedRef.current) {
        loadedRef.current = true;
        reload();
        observer.disconnect();
      }
    }, { threshold: 0.1 });
    observer.observe(el);
    return () => observer.disconnect();
  }, [reload]);

  const handleDelete = async (id) => {
    if (confirm("Delete this scene?")) {
      await deleteScene(id);
      reload();
    }
  };

  return (
    <div
      ref={containerRef}
      style={{
        display: "flex",
        flexDirection: "column",
        height: "100%",
        background: "var(--bg)",
      }}
    >
      <div
        style={{
          padding: "8px 12px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 8,
        }}
      >
        <span style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>Saved Scenes</span>
        <button
          onClick={reload}
          style={{
            marginLeft: "auto",
            padding: "3px 8px",
            fontSize: 12,
            background: "var(--panel-2)",
            border: "1px solid var(--line)",
            borderRadius: 4,
            color: "var(--ink-3)",
            cursor: "pointer",
          }}
        >
          Refresh
        </button>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 8 }}>
        {loading && (
          <div style={{ padding: 12, color: "var(--ink-3)", fontSize: 12 }}>Loading...</div>
        )}
        {!loading && scenes.length === 0 && (
          <div
            style={{
              padding: 20,
              textAlign: "center",
              color: "var(--ink-2)",
              fontSize: 12,
            }}
          >
            No saved scenes yet. Use the save button after generating a scene.
          </div>
        )}
        {scenes.map((scene) => (
          <div
            key={scene.id}
            style={{
              marginBottom: 8,
              borderRadius: 6,
              overflow: "hidden",
              border: "1px solid var(--line)",
              background: "var(--panel)",
            }}
          >
            {scene.thumbnail && (
              <div
                style={{
                  height: 100,
                  overflow: "hidden",
                  borderBottom: "1px solid var(--line)",
                }}
              >
                <img
                  src={scene.thumbnail}
                  alt={scene.name}
                  style={{ width: "100%", height: "100%", objectFit: "cover" }}
                />
              </div>
            )}
            <div style={{ padding: "8px 10px" }}>
              <div style={{ fontSize: 12, fontWeight: 500, color: "var(--ink)" }}>{scene.name}</div>
              {scene.prompt && (
                <div
                  style={{
                    fontSize: 12,
                    color: "var(--ink-3)",
                    marginTop: 3,
                    lineHeight: 1.4,
                  }}
                >
                  {scene.prompt.length > 80 ? scene.prompt.slice(0, 80) + "..." : scene.prompt}
                </div>
              )}
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 6,
                  marginTop: 6,
                }}
              >
                <span style={{ fontSize: 12, color: "var(--ink-2)" }}>
                  {new Date(scene.updatedAt).toLocaleDateString()}
                </span>
                <div style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
                  <button
                    onClick={() => onLoadScene(scene)}
                    style={{
                      padding: "2px 8px",
                      fontSize: 12,
                      borderRadius: 3,
                      border: "1px solid var(--blue)",
                      background: "transparent",
                      color: "var(--blue)",
                      cursor: "pointer",
                    }}
                  >
                    Load
                  </button>
                  <button
                    onClick={() => handleDelete(scene.id)}
                    style={{
                      padding: "2px 8px",
                      fontSize: 12,
                      borderRadius: 3,
                      border: "1px solid var(--line)",
                      background: "transparent",
                      color: "var(--red)",
                      cursor: "pointer",
                    }}
                  >
                    Del
                  </button>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─── BattleSide ──────────────────────────────────────────────────────────────

function BattleSide({ label, side, isWinner, isLoser, revealed, onVote }) {
  const borderColor = isWinner ? "#16a34a" : isLoser ? "#dc262633" : "var(--line)";

  return (
    <div
      style={{
        border: `2px solid ${borderColor}`,
        borderRadius: 12,
        overflow: "hidden",
        background: "var(--panel)",
        transition: "border-color 0.2s",
        opacity: isLoser ? 0.6 : 1,
      }}
    >
      <div
        style={{
          padding: "10px 16px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
        }}
      >
        <span style={{ fontSize: 14, fontWeight: 600, color: "var(--ink)" }}>
          {label}
          {isWinner && <span style={{ display:"inline-flex", alignItems:"center", marginLeft:4, color:"var(--green)" }}>{ICONS.check(12)}</span>}
        </span>
        {revealed && side && (
          <span
            style={{
              fontSize: 12,
              color: "var(--ink-3)",
              background: "var(--panel-2)",
              padding: "2px 8px",
              borderRadius: 4,
            }}
          >
            {side.agentName}
          </span>
        )}
        {!revealed && (
          <span style={{ fontSize: 12, color: "var(--ink-2)" }}>Identity hidden</span>
        )}
      </div>

      <div
        style={{
          height: 250,
          background: "var(--bg)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
        }}
      >
        {side && side.screenshots.length > 0 ? (
          <img
            src={side.screenshots[0]}
            alt={label}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          <div style={{ color: "var(--ink-2)", fontSize: 13 }}>
            {side ? "No screenshot available" : "Waiting for generation..."}
          </div>
        )}
      </div>

      {onVote && (
        <div style={{ padding: 12, textAlign: "center" }}>
          <button
            onClick={onVote}
            style={{
              padding: "8px 24px",
              fontSize: 13,
              fontWeight: 600,
              background: "var(--green)",
              border: "1px solid var(--green)",
              borderRadius: 6,
              color: "#fff",
              cursor: "pointer",
              width: "100%",
            }}
          >
            Vote for {label}
          </button>
        </div>
      )}
    </div>
  );
}

// ─── ArenaPage ───────────────────────────────────────────────────────────────

const tieButtonStyle = {
  padding: "8px 20px",
  fontSize: 12,
  background: "var(--panel-2)",
  border: "1px solid var(--line)",
  borderRadius: 6,
  color: "var(--ink-3)",
  cursor: "pointer",
};

function ArenaPage() {
  const [prompt, setPrompt] = useState("");
  const [battle, setBattle] = useState(null);
  const [phase, setPhase] = useState("prompt");
  const [voted, setVoted] = useState(null);
  const [progress, setProgress] = useState(null);
  const [agents, setAgents] = useState([]);
  const [showAgents, setShowAgents] = useState(false);
  const [shared, setShared] = useState(false);
  const abortRef = useRef(null);

  useEffect(() => {
    fetchAgents()
      .then(setAgents)
      .catch(() => {});
  }, []);

  const startBattle = async () => {
    if (!prompt.trim()) return;
    setPhase("generating");
    setProgress(null);
    setShared(false);

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      await runArena(
        prompt,
        [],
        (eventType, data) => {
          if (eventType === "battle_created") return;
          if (eventType === "progress") {
            setProgress(data);
          } else if (eventType === "complete") {
            setBattle(data);
            setPhase("voting");
          } else if (eventType === "error") {
            console.error("Battle error:", data);
            setPhase("prompt");
          }
        },
        controller.signal
      );
    } catch (err) {
      if (err.name !== "AbortError") console.error("Battle failed:", err);
      if (phase === "generating") setPhase("prompt");
    }
  };

  const handleVote = async (winner) => {
    if (!battle) return;
    setVoted(winner);
    try {
      const result = await voteOnBattle(battle.id, winner);
      setBattle(result);
      setPhase("result");
    } catch {}
  };

  const handleShareWinner = async () => {
    if (!battle) return;
    const winnerSide =
      voted === "a" ? battle.side_a : voted === "b" ? battle.side_b : battle.side_a;
    if (winnerSide) {
      try {
        await shareToGallery({
          prompt: battle.prompt,
          agentName: winnerSide.agentName,
          screenshots: winnerSide.screenshots,
          tags: ["arena", "battle"],
          skills: battle.skills,
        });
        setShared(true);
      } catch {}
    }
  };

  const resetBattle = () => {
    if (abortRef.current) abortRef.current.abort();
    setPrompt("");
    setBattle(null);
    setPhase("prompt");
    setVoted(null);
    setProgress(null);
    setShared(false);
  };

  const toggleAgent = async (agentId, enabled) => {
    try {
      const result = await updateAgent(agentId, { enabled });
      setAgents((prev) => prev.map((a) => (a.id === agentId ? { ...a, ...result } : a)));
    } catch {}
  };

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "var(--bg)",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.swords(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)" }}>Arena Battle</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            Two agents generate scenes from the same prompt. You decide which is better.
          </div>
        </div>
        <div style={{ marginLeft: "auto", display: "flex", gap: 8 }}>
          <button
            onClick={() => setShowAgents(!showAgents)}
            style={{
              padding: "4px 12px",
              fontSize: 12,
              borderRadius: 4,
              border: "1px solid var(--line)",
              background: showAgents ? "#eff4ff" : "#e6e9ef",
              color: showAgents ? "var(--blue)" : "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            Agents ({agents.filter((a) => a.enabled).length}/{agents.length})
          </button>
        </div>
      </div>

      {/* Agent list */}
      {showAgents && (
        <div
          style={{
            padding: "12px 24px",
            borderBottom: "1px solid var(--line)",
            background: "var(--panel)",
          }}
        >
          <div
            style={{
              fontSize: 12,
              color: "var(--ink-3)",
              marginBottom: 8,
              fontWeight: 600,
            }}
          >
            Available Agents
          </div>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
            {agents.map((agent) => (
              <div
                key={agent.id}
                style={{
                  padding: "8px 14px",
                  borderRadius: 6,
                  border: `1px solid ${agent.enabled ? "var(--green)" : "var(--line)"}`,
                  background: agent.enabled ? "var(--green-soft)" : "transparent",
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  minWidth: 200,
                }}
              >
                <input
                  type="checkbox"
                  checked={agent.enabled}
                  onChange={(e) => toggleAgent(agent.id, e.target.checked)}
                  style={{ accentColor: "#15803d" }}
                />
                <div>
                  <div style={{ fontSize: 12, fontWeight: 600, color: "var(--ink)" }}>
                    {agent.name}
                  </div>
                  <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
                    {agent.type}
                    {agent.model ? ` (${agent.model})` : ""}
                  </div>
                  {agent.description && (
                    <div style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 2 }}>
                      {agent.description}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Content area */}
      <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
        {/* Prompt phase */}
        {phase === "prompt" && (
          <div style={{ maxWidth: 600, margin: "60px auto", textAlign: "center" }}>
            <div
              style={{
                fontSize: 28,
                fontWeight: 700,
                color: "var(--ink)",
                marginBottom: 8,
              }}
            >
              Enter a Scene Prompt
            </div>
            <div style={{ fontSize: 14, color: "var(--ink-3)", marginBottom: 24 }}>
              Both agents will try to build this scene. Vote for the better result.
            </div>
            <textarea
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="A quiet residential neighborhood with tree-lined streets and a small park..."
              style={{
                width: "100%",
                height: 100,
                padding: 14,
                fontSize: 14,
                background: "var(--panel)",
                border: "1px solid var(--line)",
                borderRadius: 8,
                color: "var(--ink)",
                resize: "vertical",
                outline: "none",
                boxSizing: "border-box",
                lineHeight: 1.5,
              }}
            />
            <div
              style={{
                display: "flex",
                gap: 12,
                justifyContent: "center",
                marginTop: 16,
              }}
            >
              <button
                onClick={startBattle}
                disabled={!prompt.trim()}
                style={{
                  padding: "10px 28px",
                  fontSize: 14,
                  background: prompt.trim() ? "#15803d" : "#e6e9ef",
                  border: `1px solid ${prompt.trim() ? "var(--green)" : "var(--line)"}`,
                  borderRadius: 8,
                  color: prompt.trim() ? "#fff" : "#94a3b8",
                  cursor: prompt.trim() ? "pointer" : "default",
                  fontWeight: 600,
                }}
              >
                Start Battle
              </button>
            </div>
            <div style={{ fontSize: 12, color: "var(--ink-2)", marginTop: 12 }}>
              {agents.filter((a) => a.enabled).length} agent
              {agents.filter((a) => a.enabled).length !== 1 ? "s" : ""} enabled
            </div>
          </div>
        )}

        {/* Generating phase */}
        {phase === "generating" && (
          <div style={{ textAlign: "center", padding: 80 }}>
            <div style={{ fontSize: 40, marginBottom: 16 }}>{ICONS.swords(40)}</div>
            <div style={{ fontSize: 16, color: "var(--ink)", fontWeight: 600 }}>
              Generating scenes...
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)", marginTop: 8 }}>
              "{prompt}"
            </div>
            {progress && (
              <div style={{ marginTop: 20 }}>
                {progress.phase === "starting" && (
                  <div style={{ fontSize: 12, color: "var(--blue)" }}>
                    Matched: {progress.agentA} vs {progress.agentB}
                  </div>
                )}
                {progress.phase === "generating_a" && (
                  <div style={{ fontSize: 12, color: "var(--green)" }}>
                    Agent A ({progress.agent}) is generating...
                  </div>
                )}
                {progress.phase === "generating_b" && (
                  <div style={{ fontSize: 12, color: "var(--green)" }}>
                    Agent A done. Agent B ({progress.agent}) is generating...
                  </div>
                )}
              </div>
            )}
            <div
              style={{
                marginTop: 24,
                width: 200,
                height: 4,
                background: "var(--panel-2)",
                borderRadius: 2,
                overflow: "hidden",
                margin: "24px auto 0",
              }}
            >
              <div
                style={{
                  width: progress?.phase === "generating_b" ? "80%" : "40%",
                  height: "100%",
                  background: "var(--blue)",
                  borderRadius: 2,
                  transition: "width 0.5s",
                }}
              />
            </div>
            <button
              onClick={resetBattle}
              style={{
                marginTop: 24,
                padding: "6px 16px",
                fontSize: 12,
                background: "transparent",
                border: "1px solid var(--line)",
                borderRadius: 6,
                color: "var(--ink-3)",
                cursor: "pointer",
              }}
            >
              Cancel
            </button>
          </div>
        )}

        {/* Voting / Result phase */}
        {(phase === "voting" || phase === "result") && battle && (
          <div>
            <div style={{ textAlign: "center", marginBottom: 20 }}>
              <div style={{ fontSize: 13, color: "var(--ink-3)" }}>Prompt</div>
              <div style={{ fontSize: 16, color: "var(--ink)", fontWeight: 500 }}>
                "{battle.prompt}"
              </div>
            </div>

            <div
              style={{
                display: "grid",
                gridTemplateColumns: "1fr 1fr",
                gap: 16,
                maxWidth: 1000,
                margin: "0 auto",
              }}
            >
              <BattleSide
                label="Agent A"
                side={battle.side_a}
                isWinner={voted === "a" || battle.winner === "a"}
                isLoser={voted !== null && voted !== "a" && voted !== "tie" && voted !== "both_bad"}
                revealed={phase === "result"}
                onVote={phase === "voting" ? () => handleVote("a") : undefined}
              />
              <BattleSide
                label="Agent B"
                side={battle.side_b}
                isWinner={voted === "b" || battle.winner === "b"}
                isLoser={voted !== null && voted !== "b" && voted !== "tie" && voted !== "both_bad"}
                revealed={phase === "result"}
                onVote={phase === "voting" ? () => handleVote("b") : undefined}
              />
            </div>

            {phase === "voting" && (
              <div
                style={{
                  display: "flex",
                  gap: 12,
                  justifyContent: "center",
                  marginTop: 20,
                }}
              >
                <button onClick={() => handleVote("tie")} style={tieButtonStyle}>
                  Tie - Both Good
                </button>
                <button onClick={() => handleVote("both_bad")} style={tieButtonStyle}>
                  Both Bad
                </button>
              </div>
            )}

            {phase === "result" && (
              <div style={{ textAlign: "center", marginTop: 24 }}>
                <div
                  style={{
                    fontSize: 14,
                    color: "var(--green)",
                    fontWeight: 600,
                    marginBottom: 12,
                  }}
                >
                  {voted === "tie"
                    ? "You voted: Tie"
                    : voted === "both_bad"
                      ? "You voted: Both Bad"
                      : `You voted: Agent ${voted?.toUpperCase()} wins!`}
                </div>
                {battle.side_a && battle.side_b && (
                  <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
                    Agent A: {battle.side_a.agentName} | Agent B: {battle.side_b.agentName}
                  </div>
                )}
                <div
                  style={{
                    display: "flex",
                    gap: 12,
                    justifyContent: "center",
                    marginTop: 16,
                  }}
                >
                  <button
                    onClick={resetBattle}
                    style={{
                      padding: "8px 20px",
                      fontSize: 13,
                      background: "var(--blue)",
                      border: "1px solid #388bfd",
                      borderRadius: 6,
                      color: "#fff",
                      cursor: "pointer",
                    }}
                  >
                    New Battle
                  </button>
                  <button
                    onClick={handleShareWinner}
                    disabled={shared}
                    style={{
                      padding: "8px 20px",
                      fontSize: 13,
                      background: shared ? "var(--panel-2)" : "var(--green)",
                      border: `1px solid ${shared ? "var(--line)" : "var(--green)"}`,
                      borderRadius: 6,
                      color: shared ? "var(--ink-3)" : "#fff",
                      cursor: shared ? "default" : "pointer",
                    }}
                  >
                    {shared ? "Shared!" : "Share to Gallery"}
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── LeaderboardPage ─────────────────────────────────────────────────────────

function LeaderboardPage() {
  const [entries, setEntries] = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchLeaderboard()
      .then(setEntries)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, []);

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "var(--bg)",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.trophy(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)" }}>Leaderboard</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            Agent rankings based on Elo ratings from arena battles
          </div>
        </div>
        {entries.length > 0 && (
          <div style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-2)" }}>
            {entries.reduce((sum, e) => sum + e.numBattles, 0)} total battles
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 24 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "var(--ink-3)" }}>Loading...</div>
        ) : entries.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.trophy(40)}</div>
            <div
              style={{
                fontSize: 16,
                color: "var(--ink)",
                fontWeight: 600,
                marginBottom: 8,
              }}
            >
              No battles yet
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)" }}>
              Run arena battles to see agents compete and build the leaderboard.
            </div>
          </div>
        ) : (
          <div style={{ maxWidth: 800, margin: "0 auto" }}>
            {/* Table header */}
            <div
              style={{
                display: "grid",
                gridTemplateColumns: "40px 1fr 100px 80px 80px 80px 80px",
                padding: "8px 16px",
                fontSize: 12,
                color: "var(--ink-2)",
                fontWeight: 600,
                borderBottom: "1px solid var(--line)",
                textTransform: "uppercase",
              }}
            >
              <span>#</span>
              <span>Agent</span>
              <span style={{ textAlign: "right" }}>Rating</span>
              <span style={{ textAlign: "right" }}>Battles</span>
              <span style={{ textAlign: "right" }}>Wins</span>
              <span style={{ textAlign: "right" }}>Losses</span>
              <span style={{ textAlign: "right" }}>Win Rate</span>
            </div>

            {/* Rows */}
            {entries.map((entry, i) => {
              const delta = entry.rating - 1200;
              return (
                <div
                  key={entry.agentName}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "40px 1fr 100px 80px 80px 80px 80px",
                    padding: "12px 16px",
                    alignItems: "center",
                    borderBottom: "1px solid var(--line)",
                    background: i === 0 ? "var(--blue-soft)" : "transparent",
                  }}
                >
                  <span
                    style={{
                      fontSize: 14,
                      fontWeight: 700,
                      color:
                        i === 0
                          ? "#f0c000"
                          : i === 1
                            ? "#c0c0c0"
                            : i === 2
                              ? "#cd7f32"
                              : "#94a3b8",
                    }}
                  >
                    {i === 0 ? ICONS.gold(18) : i === 1 ? ICONS.silver(18) : i === 2 ? ICONS.bronze(18) : i + 1}
                  </span>
                  <div>
                    <span style={{ fontSize: 14, fontWeight: 600, color: "var(--ink)" }}>
                      {entry.agentName}
                    </span>
                    {i === 0 && (
                      <span
                        style={{
                          marginLeft: 8,
                          fontSize: 12,
                          padding: "1px 6px",
                          background: "#f0c00022",
                          color: "#f0c000",
                          borderRadius: 4,
                          border: "1px solid #f0c00044",
                        }}
                      >
                        Champion
                      </span>
                    )}
                  </div>
                  <div style={{ textAlign: "right" }}>
                    <span
                      style={{
                        fontSize: 16,
                        fontWeight: 700,
                        color:
                          entry.rating >= 1200
                            ? "#16a34a"
                            : entry.rating >= 1000
                              ? "#0f172a"
                              : "#dc2626",
                      }}
                    >
                      {Math.round(entry.rating)}
                    </span>
                    <span
                      style={{
                        fontSize: 12,
                        marginLeft: 4,
                        color: delta >= 0 ? "#16a34a" : "#dc2626",
                      }}
                    >
                      {delta >= 0 ? "+" : ""}
                      {delta}
                    </span>
                  </div>
                  <span style={{ textAlign: "right", fontSize: 13, color: "var(--ink-3)" }}>
                    {entry.numBattles}
                  </span>
                  <span style={{ textAlign: "right", fontSize: 13, color: "var(--green)" }}>
                    {entry.wins}
                  </span>
                  <span style={{ textAlign: "right", fontSize: 13, color: "var(--red)" }}>
                    {entry.losses}
                  </span>
                  <span
                    style={{
                      textAlign: "right",
                      fontSize: 13,
                      fontWeight: 600,
                      color:
                        entry.winRate >= 0.6
                          ? "#16a34a"
                          : entry.winRate >= 0.4
                            ? "#0f172a"
                            : "#dc2626",
                    }}
                  >
                    {(entry.winRate * 100).toFixed(1)}%
                  </span>
                </div>
              );
            })}

            {/* Info box */}
            <div
              style={{
                marginTop: 24,
                padding: 16,
                borderRadius: 8,
                background: "var(--panel)",
                border: "1px solid var(--line)",
                fontSize: 12,
                color: "var(--ink-3)",
                lineHeight: 1.6,
              }}
            >
              <div style={{ fontWeight: 600, marginBottom: 4, color: "var(--ink)" }}>
                How ratings work
              </div>
              Agents start at 1200 Elo. Each battle updates ratings using the Bradley-Terry model
              (K=32). Winning against a higher-rated agent gives more points. Ties give half credit.
              Run more battles for more accurate rankings.
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── GalleryCard ─────────────────────────────────────────────────────────────

function GalleryCard({ scene, onClick }) {
  return (
    <div
      onClick={onClick}
      style={{
        borderRadius: 10,
        overflow: "hidden",
        border: "1px solid var(--line)",
        background: "var(--panel)",
        cursor: "pointer",
        transition: "border-color 0.15s, transform 0.15s",
      }}
      onMouseEnter={(e) => {
        e.currentTarget.style.borderColor = "var(--blue)";
        e.currentTarget.style.transform = "translateY(-2px)";
      }}
      onMouseLeave={(e) => {
        e.currentTarget.style.borderColor = "var(--line)";
        e.currentTarget.style.transform = "translateY(0)";
      }}
    >
      <div style={{ height: 180, background: "var(--bg)", overflow: "hidden" }}>
        {scene.screenshots.length > 0 ? (
          <img
            src={scene.screenshots[0]}
            alt={scene.prompt}
            style={{ width: "100%", height: "100%", objectFit: "cover" }}
          />
        ) : (
          <div
            style={{
              height: "100%",
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--ink-2)",
              fontSize: 13,
            }}
          >
            No preview
          </div>
        )}
      </div>
      <div style={{ padding: "10px 14px" }}>
        <div
          style={{
            fontSize: 13,
            fontWeight: 500,
            color: "var(--ink)",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            lineHeight: 1.4,
          }}
        >
          {scene.prompt}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            marginTop: 8,
          }}
        >
          <span
            style={{
              fontSize: 12,
              padding: "2px 6px",
              borderRadius: 4,
              background: "var(--blue-soft)",
              color: "var(--blue)",
              border: "1px solid rgba(76,141,255,0.26)",
            }}
          >
            {scene.agentName}
          </span>
          {scene.tags.slice(0, 2).map((tag) => (
            <span
              key={tag}
              style={{
                fontSize: 12,
                padding: "1px 5px",
                borderRadius: 4,
                background: "var(--panel-2)",
                color: "var(--ink-3)",
              }}
            >
              {tag}
            </span>
          ))}
          <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-2)" }}>
            {new Date(scene.created_at).toLocaleDateString()}
          </span>
        </div>
      </div>
    </div>
  );
}

// ─── GalleryDetailModal ──────────────────────────────────────────────────────

function GalleryDetailModal({ scene, onClose }) {
  const [currentImg, setCurrentImg] = useState(0);

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 900,
          maxHeight: "85vh",
          background: "var(--bg)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 16, fontWeight: 600, color: "var(--ink)" }}>{scene.prompt}</div>
            <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 4 }}>
              Generated by {scene.agentName} on {new Date(scene.created_at).toLocaleString()}
            </div>
          </div>
          {onBack ? (
            <button
              onClick={onBack}
              style={{
                padding: "4px 10px",
                fontSize: 12,
                background: "var(--panel-2)",
                border: "1px solid var(--line)",
                borderRadius: 6,
                color: "var(--ink)",
                cursor: "pointer",
              }}
            >
              ← Back
            </button>
          ) : (
            <button
              onClick={onClose}
              style={{
                padding: "4px 10px",
                fontSize: 16,
                background: "transparent",
                border: "none",
                color: "var(--ink-3)",
                cursor: "pointer",
              }}
            >
              ✕
            </button>
          )}
        </div>

        {/* Image */}
        <div style={{ position: "relative", background: "#000" }}>
          {scene.screenshots.length > 0 ? (
            <img
              src={scene.screenshots[currentImg]}
              alt={`Screenshot ${currentImg + 1}`}
              style={{ width: "100%", maxHeight: 400, objectFit: "contain" }}
            />
          ) : (
            <div
              style={{
                height: 200,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--ink-2)",
              }}
            >
              No screenshots
            </div>
          )}
          {scene.screenshots.length > 1 && (
            <div
              style={{
                position: "absolute",
                bottom: 8,
                left: "50%",
                transform: "translateX(-50%)",
                display: "flex",
                gap: 6,
              }}
            >
              {scene.screenshots.map((_, i) => (
                <button
                  key={i}
                  onClick={() => setCurrentImg(i)}
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    border: "none",
                    background: i === currentImg ? "var(--blue)" : "var(--ink-3)",
                    cursor: "pointer",
                  }}
                />
              ))}
            </div>
          )}
        </div>

        {/* Details */}
        <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 12 }}>
            {scene.skills.map((s) => (
              <span
                key={s}
                style={{
                  fontSize: 12,
                  padding: "2px 7px",
                  borderRadius: 4,
                  background: "var(--blue-soft)",
                  color: "var(--blue)",
                  border: "1px solid rgba(76,141,255,0.26)",
                }}
              >
                {s}
              </span>
            ))}
            {scene.tags.map((tag) => (
              <span
                key={tag}
                style={{
                  fontSize: 12,
                  padding: "2px 7px",
                  borderRadius: 4,
                  background: "var(--panel-2)",
                  color: "var(--ink-3)",
                }}
              >
                {tag}
              </span>
            ))}
          </div>
          {scene.codePreview && (
            <div>
              <div
                style={{
                  fontSize: 12,
                  color: "var(--ink-3)",
                  fontWeight: 600,
                  marginBottom: 6,
                }}
              >
                Generated Code Preview
              </div>
              <pre
                style={{
                  fontSize: 12,
                  color: "var(--ink)",
                  background: "var(--panel)",
                  border: "1px solid var(--line)",
                  borderRadius: 6,
                  padding: 12,
                  overflow: "auto",
                  maxHeight: 200,
                  fontFamily:
                    "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
                  lineHeight: 1.5,
                  whiteSpace: "pre-wrap",
                }}
              >
                {scene.codePreview}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── GalleryPage ─────────────────────────────────────────────────────────────

function GalleryPage() {
  const [scenes, setScenes] = useState([]);
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState(null);
  const [sortOrder, setSortOrder] = useState("newest");
  const [activeTag, setActiveTag] = useState(null);

  useEffect(() => {
    setLoading(true);
    fetchGallery({ limit: 100, sort: sortOrder })
      .then(setScenes)
      .catch(() => {})
      .finally(() => setLoading(false));
  }, [sortOrder]);

  const allTags = Array.from(new Set(scenes.flatMap((s) => s.tags || [])));
  const filtered = activeTag
    ? scenes.filter((s) => s.tags?.includes(activeTag))
    : scenes;

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "var(--bg)",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.frame(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)" }}>Gallery</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            Browse AI-generated scenes shared by the community
          </div>
        </div>
        <div
          style={{
            marginLeft: "auto",
            display: "flex",
            gap: 8,
            alignItems: "center",
          }}
        >
          <select
            value={sortOrder}
            onChange={(e) => setSortOrder(e.target.value)}
            style={{
              padding: "3px 8px",
              fontSize: 12,
              borderRadius: 4,
              border: "1px solid var(--line)",
              background: "var(--panel)",
              color: "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            <option value="newest">Newest first</option>
            <option value="oldest">Oldest first</option>
          </select>
          <span style={{ fontSize: 12, color: "var(--ink-2)" }}>
            {filtered.length} scene{filtered.length !== 1 ? "s" : ""}
          </span>
        </div>
      </div>

      {/* Tag filter */}
      {allTags.length > 0 && (
        <div
          style={{
            padding: "8px 24px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            gap: 6,
            flexWrap: "wrap",
            alignItems: "center",
          }}
        >
          <span style={{ fontSize: 12, color: "var(--ink-2)", marginRight: 4 }}>Tags:</span>
          <button
            onClick={() => setActiveTag(null)}
            style={{
              fontSize: 12,
              padding: "2px 8px",
              borderRadius: 10,
              border: `1px solid ${activeTag ? "var(--line)" : "var(--blue)"}`,
              background: activeTag ? "transparent" : "var(--blue-soft)",
              color: activeTag ? "var(--ink-3)" : "var(--blue)",
              cursor: "pointer",
            }}
          >
            All
          </button>
          {allTags.slice(0, 15).map((tag) => (
            <button
              key={tag}
              onClick={() => setActiveTag(activeTag === tag ? null : tag)}
              style={{
                fontSize: 12,
                padding: "2px 8px",
                borderRadius: 10,
                border: `1px solid ${activeTag === tag ? "var(--blue)" : "var(--line)"}`,
                background: activeTag === tag ? "var(--blue-soft)" : "transparent",
                color: activeTag === tag ? "var(--blue)" : "var(--ink-3)",
                cursor: "pointer",
              }}
            >
              {tag}
            </button>
          ))}
        </div>
      )}

      {/* Content */}
      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "var(--ink-3)" }}>Loading...</div>
        ) : filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.frame(40)}</div>
            <div
              style={{
                fontSize: 16,
                color: "var(--ink)",
                fontWeight: 600,
                marginBottom: 8,
              }}
            >
              No scenes yet
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)" }}>
              Generate scenes in the chat and share them to the gallery, or run arena battles.
            </div>
          </div>
        ) : (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(280px, 1fr))",
              gap: 16,
            }}
          >
            {filtered.map((s) => (
              <GalleryCard key={s.id} scene={s} onClick={() => setSelected(s)} />
            ))}
          </div>
        )}
      </div>

      {selected && <GalleryDetailModal scene={selected} onClose={() => setSelected(null)} />}
    </div>
  );
}

// ─── SkillsPage (full page) ──────────────────────────────────────────────────

function SkillPageCard({ skill, onClick, isNew = false }) {
  const desc = skill.description.length > 120
    ? skill.description.slice(0, 120) + "…" : skill.description;
  return (
    <div onClick={onClick} style={{
        padding: "14px 16px", borderRadius: 10, border: "1px solid var(--line)",
        background: "var(--panel)", cursor: "pointer",
        transition: "border-color 0.15s, box-shadow 0.15s",
        display: "flex", flexDirection: "column", gap: 8,
      }}
      onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--blue)"; e.currentTarget.style.boxShadow = "0 2px 12px rgba(76,141,255,0.1)"; }}
      onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--line)";  e.currentTarget.style.boxShadow = "none"; }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {skill.name}
        </span>
        {isNew && <Badge variant="red" dot>NEW</Badge>}
        <SourceBadge source={skill.source} />
      </div>
      <div style={{ fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>{desc}</div>
      {skill.tags.length > 0 && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
          {skill.tags.map(tag => <TagChip key={tag} tag={tag} />)}
        </div>
      )}
      <div style={{
        display: "flex", alignItems: "center", gap: 8, marginTop: "auto",
        paddingTop: 4, borderTop: "1px solid var(--line)", fontSize: 11, color: "var(--ink-3)",
      }}>
        <span>v{skill.version}</span>
        <span style={{ marginLeft: "auto" }}>{skill.author}</span>
      </div>
    </div>
  );
}

function SkillPageDetailModal({ skill, onClose, onDelete }) {
  return (
    <ModalOverlay onClose={onClose}>
      <ModalHeader
        title={skill.name}
        subtitle={<>v{skill.version} by {skill.author} <SourceBadge source={skill.source} style={{ marginLeft: 6 }} /></>}
        onClose={onClose}
      >
        {onDelete && <Btn variant="danger" onClick={onDelete}>Delete</Btn>}
      </ModalHeader>

      <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--line)" }}>
        <div style={{ fontSize: 13, color: "var(--ink)", lineHeight: 1.6 }}>{skill.description}</div>
        {skill.tags.length > 0 && (
          <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 10 }}>
            {skill.tags.map(tag => <TagChip key={tag} tag={tag} />)}
          </div>
        )}
        {skill.dependencies.length > 0 && (
          <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 10 }}>
            <span style={{ fontWeight: 600 }}>Dependencies: </span>
            {skill.dependencies.map((dep, i) => (
              <span key={dep}>
                <span style={{ color: "var(--blue)" }}>{dep}</span>
                {i < skill.dependencies.length - 1 ? ", " : ""}
              </span>
            ))}
          </div>
        )}
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: "14px 20px" }}>
        <Eyebrow style={{ marginBottom: 8 }}>Skill Content</Eyebrow>
        <pre style={{
          fontSize: 12, color: "var(--ink-2)", lineHeight: 1.6,
          whiteSpace: "pre-wrap", wordBreak: "break-word",
          fontFamily: "ui-monospace, 'Cascadia Code', Menlo, monospace",
          margin: 0, background: "var(--bg-tertiary)",
          border: "1px solid var(--line)", borderRadius: 8, padding: 16,
        }}>
          {skill.content}
        </pre>
      </div>
    </ModalOverlay>
  );
}

function SkillPageCreateModal({ onClose, onCreated }) {
  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [tags, setTags] = useState("");
  const [content, setContent] = useState("# My Custom Skill\n\n## Overview\nDescribe what this skill does.\n\n## Instructions\nProvide detailed instructions for the AI agent.\n");
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);

  const handleSave = async () => {
    if (!id.trim() || !name.trim() || !content.trim()) { setError("ID, name, and content are required"); return; }
    if (!/^[a-z0-9_]+$/.test(id)) { setError("ID must be lowercase letters, numbers, and underscores only"); return; }
    setSaving(true);
    try {
      await createSkill({ id: id.trim(), name: name.trim(), description: description.trim(),
        tags: tags.split(",").map(t => t.trim()).filter(Boolean), content: content.trim() });
      onCreated();
    } catch { setError("Failed to save skill"); } finally { setSaving(false); }
  };

  return (
    <ModalOverlay onClose={onClose} maxWidth={650}>
      <ModalHeader title="Create Custom Skill" onClose={onClose} />

      <div style={{ flex: 1, overflow: "auto", padding: "16px 20px", display: "flex", flexDirection: "column", gap: 14 }}>
        <Field label="Skill ID (lowercase, no spaces)">
          <input value={id} onChange={e => setId(e.target.value)} placeholder="my_custom_skill" style={inputSx} />
        </Field>
        <Field label="Name">
          <input value={name} onChange={e => setName(e.target.value)} placeholder="My Custom Skill" style={inputSx} />
        </Field>
        <Field label="Description (short summary)">
          <input value={description} onChange={e => setDescription(e.target.value)} placeholder="What this skill teaches the agent to do" style={inputSx} />
        </Field>
        <Field label="Tags (comma-separated)">
          <input value={tags} onChange={e => setTags(e.target.value)} placeholder="buildings, layout, custom" style={inputSx} />
        </Field>
        <Field label="Content (Markdown — instructions for the AI agent)" style={{ flex: 1 }}>
          <textarea value={content} onChange={e => setContent(e.target.value)}
            style={{ ...inputSx, height: 200, resize: "vertical", fontFamily: "ui-monospace, Menlo, monospace", lineHeight: 1.5 }} />
        </Field>
        {error && <div style={{ fontSize: 12, color: "var(--red)" }}>{error}</div>}
      </div>

      <ModalFooter>
        <Btn variant="cancel" onClick={onClose}>Cancel</Btn>
        <Btn variant="success" disabled={saving} onClick={handleSave}>{saving ? "Saving…" : "Create Skill"}</Btn>
      </ModalFooter>
    </ModalOverlay>
  );
}

function ConfirmDeleteModal({ message, onConfirm, onCancel }) {
  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 10000,
      }}
      onClick={onCancel}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 400,
          background: "var(--bg)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          padding: 24,
        }}
      >
        <div style={{ fontSize: 14, color: "var(--ink)", fontWeight: 600, marginBottom: 8 }}>
          Confirm Delete
        </div>
        <div style={{ fontSize: 13, color: "var(--ink-3)", lineHeight: 1.5, marginBottom: 20 }}>
          {message}
        </div>
        <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
          <button
            onClick={onCancel}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "var(--panel-2)",
              border: "1px solid var(--line)",
              borderRadius: 6,
              color: "var(--ink)",
              cursor: "pointer",
            }}
          >
            Cancel
          </button>
          <button
            onClick={onConfirm}
            style={{
              padding: "6px 14px",
              fontSize: 12,
              background: "var(--red)",
              border: "1px solid rgba(255,95,99,0.3)",
              borderRadius: 6,
              color: "#fff",
              cursor: "pointer",
              fontWeight: 600,
            }}
          >
            Delete
          </button>
        </div>
      </div>
    </div>
  );
}

function SkillsPage({ newlyAddedSkillIds = [], onMarkSkillSeen }) {
  const [skills, setSkills] = useState([]);
  const [loading, setLoading] = useState(true);
  const [search, setSearch] = useState("");
  const [filter, setFilter] = useState("all");
  const [previewSkill, setPreviewSkill] = useState(null);
  const [showCreate, setShowCreate] = useState(false);
  const [deleteId, setDeleteId] = useState(null);

  const reload = () => {
    setLoading(true);
    fetchSkills()
      .then(setSkills)
      .catch(() => {})
      .finally(() => setLoading(false));
  };

  useEffect(reload, []);

  const handlePreview = async (id) => {
    const normalizedId = String(id || "").trim();
    if (normalizedId && typeof onMarkSkillSeen === "function") {
      onMarkSkillSeen(normalizedId);
    }
    try {
      const detail = await fetchSkillDetails(id);
      setPreviewSkill(detail);
    } catch {}
  };

  const handleDelete = async (id) => {
    await deleteSkill(id);
    setPreviewSkill(null);
    setDeleteId(null);
    reload();
  };

  const filtered = skills.filter((s) => {
    if (filter !== "all" && s.source !== filter) return false;
    if (!search.trim()) return true;
    const q = search.toLowerCase();
    return (
      s.name.toLowerCase().includes(q) ||
      s.description.toLowerCase().includes(q) ||
      s.tags.some((t) => t.toLowerCase().includes(q))
    );
  });

  const newlyAddedSkillIdSet = useMemo(
    () =>
      new Set(
        (Array.isArray(newlyAddedSkillIds) ? newlyAddedSkillIds : [])
          .map((id) => String(id || "").trim())
          .filter(Boolean)
      ),
    [newlyAddedSkillIds]
  );

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "var(--bg)",
      }}
    >
      <PageHeader
        icon={ICONS.book(22)}
        title="Skills"
        subtitle="Browse, create, and manage skills that teach the AI agent new capabilities"
        action={<Btn variant="success" size="md" onClick={() => setShowCreate(true)}>+ Create Skill</Btn>}
      />

      <div
        style={{
          padding: "10px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          gap: 10,
          alignItems: "center",
        }}
      >
        <input type="text" value={search} onChange={e => setSearch(e.target.value)}
          placeholder="Search skills…" style={{ ...inputSx, flex: 1 }} />
        <div style={{ display: "flex", gap: 4 }}>
          {["all", "builtin", "custom"].map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className={`filter-pill${filter === f ? " active" : ""}`}>
              {f}
            </button>
          ))}
        </div>
        <span style={{ fontSize: 12, color: "var(--ink-2)", whiteSpace: "nowrap" }}>
          {filtered.length} skill{filtered.length !== 1 ? "s" : ""}
        </span>
      </div>

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        {loading ? (
          <div style={{ textAlign: "center", padding: 60, color: "var(--ink-3)" }}>Loading...</div>
        ) : filtered.length === 0 ? (
          <div style={{ textAlign: "center", padding: 60 }}>
            <div style={{ fontSize: 40, marginBottom: 12 }}>{ICONS.book(40)}</div>
            <div style={{ fontSize: 16, color: "var(--ink)", fontWeight: 600, marginBottom: 8 }}>
              No skills found
            </div>
            <div style={{ fontSize: 13, color: "var(--ink-3)" }}>
              {search ? "Try a different search term." : "Create a custom skill to get started."}
            </div>
          </div>
        ) : (
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
              gap: 16,
            }}
          >
            {filtered.map((s) => (
              <SkillPageCard
                key={s.id}
                skill={s}
                isNew={newlyAddedSkillIdSet.has(String(s?.id || "").trim())}
                onClick={() => handlePreview(s.id)}
              />
            ))}
          </div>
        )}
      </div>

      {previewSkill && (
        <SkillPageDetailModal
          skill={previewSkill}
          onClose={() => setPreviewSkill(null)}
          onDelete={previewSkill.source === "custom" ? () => setDeleteId(previewSkill.id) : undefined}
        />
      )}
      {deleteId && (
        <ConfirmDeleteModal
          message="Are you sure you want to delete this skill? This cannot be undone."
          onConfirm={() => handleDelete(deleteId)}
          onCancel={() => setDeleteId(null)}
        />
      )}
      {showCreate && (
        <SkillPageCreateModal
          onClose={() => setShowCreate(false)}
          onCreated={() => {
            setShowCreate(false);
            reload();
          }}
        />
      )}
    </div>
  );
}


function ToolPageCard({ tool, onClick, busy, onToggleEnabled, onDelete, isNew = false }) {
  const successRate = tool.metrics?.usageCount > 0
    ? Math.round((tool.metrics.successCount / tool.metrics.usageCount) * 100) : null;

  return (
    <div onClick={onClick} style={{
        padding: "14px 16px", borderRadius: 10, border: "1px solid var(--line)",
        background: "var(--panel)", cursor: "pointer",
        transition: "border-color 0.15s, box-shadow 0.15s",
        display: "flex", flexDirection: "column", gap: 8,
      }}
      onMouseEnter={e => { e.currentTarget.style.borderColor = "var(--blue)"; e.currentTarget.style.boxShadow = "0 2px 12px rgba(76,141,255,0.1)"; }}
      onMouseLeave={e => { e.currentTarget.style.borderColor = "var(--line)";  e.currentTarget.style.boxShadow = "none"; }}
    >
      {/* Header row */}
      <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--ink)", flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {tool.name || tool.id}
        </span>
        {isNew && <Badge variant="red" dot>NEW</Badge>}
        <StatusBadge enabled={tool.enabled} />
      </div>

      {/* Metrics grid */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 3, paddingTop: 4, borderTop: "1px solid var(--line)", fontSize: 11, color: "var(--ink-3)" }}>
        <div>Template: <span style={{ color: "var(--ink)" }}>{tool.template || "–"}</span></div>
        <div>Primitive: <span style={{ color: "var(--ink)" }}>{tool.primitive || "–"}</span></div>
        <div>Usage: <span style={{ color: "var(--ink)" }}>{tool.metrics?.usageCount || 0}</span></div>
        <div>Success: <span style={{ color: "var(--ink)" }}>{successRate == null ? "–" : `${successRate}%`}</span></div>
      </div>

      {/* Actions */}
      <div style={{ display: "flex", gap: 6, marginTop: "auto" }}>
        <ToggleBtn enabled={tool.enabled} busy={busy} onClick={e => { e.stopPropagation(); onToggleEnabled(); }} />
        <Btn variant="danger" disabled={busy} onClick={e => { e.stopPropagation(); onDelete(); }}>Delete</Btn>
      </div>
    </div>
  );
}

function StaticToolRefCard({ tool }) {
  return (
    <div
      style={{
        padding: "10px 12px",
        borderRadius: 8,
        border: "1px solid var(--line)",
        background: "var(--panel)",
        display: "flex",
        alignItems: "center",
        gap: 8,
        cursor: "pointer",
      }}
    >
      <span style={{ color: "var(--ink)", fontSize: 12, fontWeight: 600 }}>{tool.name}</span>
      <span
        style={{
          marginLeft: "auto",
          fontSize: 12,
          color: "var(--ink-3)",
          border: "1px solid var(--line)",
          borderRadius: 10,
          padding: "2px 7px",
        }}
      >
        Reference
      </span>
    </div>
  );
}

function ToolDetailModal({ tool, relatedSkills, busy, onClose, onToggleEnabled, onDelete, onOpenSkill, readOnly = false }) {
  const successRate =
    tool.metrics?.usageCount > 0
      ? Math.round((tool.metrics.successCount / tool.metrics.usageCount) * 100)
      : null;
  const schema = tool.paramsSchema || { type: "object", properties: {} };
  const schemaProps = schema.properties || {};
  const requiredKeys = Array.isArray(schema.required) ? schema.required : [];

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.8)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 9999,
      }}
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: "90%",
          maxWidth: 750,
          maxHeight: "85vh",
          background: "var(--bg)",
          border: "1px solid var(--line)",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: "14px 20px",
            borderBottom: "1px solid var(--line)",
            display: "flex",
            alignItems: "center",
            gap: 10,
          }}
        >
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 18, fontWeight: 600, color: "var(--ink)" }}>{tool.name || tool.id}</div>
            <div style={{ fontSize: 12, color: "var(--ink-3)", marginTop: 4 }}>
              <span style={{ color: "var(--blue)", fontFamily: "monospace" }}>{tool.mcpName}</span>
              <span
                style={{
                  marginLeft: 8,
                  padding: "2px 7px",
                  borderRadius: 10,
                  border: "1px solid var(--line)",
                  background: readOnly ? "var(--blue-soft)" : tool.enabled ? "var(--green-soft)" : "transparent",
                  color: readOnly ? "var(--blue)" : tool.enabled ? "var(--green)" : "var(--ink-3)",
                  fontSize: 12,
                }}
              >
                {readOnly ? "Static MCP" : tool.enabled ? "Enabled" : "Disabled"}
              </span>
            </div>
          </div>
          {!readOnly && (
            <button
              onClick={onDelete}
              disabled={busy}
              style={{
                padding: "5px 12px",
                fontSize: 12,
                background: "rgba(255,95,99,0.1)",
                border: "1px solid rgba(255,95,99,0.3)",
                borderRadius: 6,
                color: "var(--red)",
                cursor: busy ? "wait" : "pointer",
              }}
            >
              Delete
            </button>
          )}
          <button
            onClick={onClose}
            style={{
              padding: "4px 10px",
              fontSize: 16,
              background: "transparent",
              border: "none",
              color: "var(--ink-3)",
              cursor: "pointer",
            }}
          >
            ✕
          </button>
        </div>

        <div style={{ padding: "12px 20px", borderBottom: "1px solid var(--line)" }}>
          <div style={{ fontSize: 13, color: "var(--ink)", lineHeight: 1.5 }}>
            {tool.description || "No description"}
          </div>
          {!readOnly && (
            <div style={{ marginTop: 10, fontSize: 12, color: "var(--ink-3)" }}>
              Related skills:{" "}
              <span style={{ color: "var(--ink)" }}>{relatedSkills && relatedSkills.length > 0 ? relatedSkills.length : 0}</span>
            </div>
          )}
          {!readOnly && relatedSkills && relatedSkills.length > 0 && (
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginTop: 8 }}>
              {relatedSkills.map((skill) => (
                <button
                  key={skill.id || skill.name}
                  onClick={() => onOpenSkill && onOpenSkill(skill.id)}
                  style={{
                    fontSize: 12,
                    padding: "2px 7px",
                    borderRadius: 10,
                    background: "var(--blue-soft)",
                    color: "var(--blue)",
                    border: "1px solid rgba(76,141,255,0.26)",
                    cursor: "pointer",
                  }}
                >
                  {skill.name}
                </button>
              ))}
            </div>
          )}
        </div>

        <div style={{ flex: 1, overflow: "auto", padding: "14px 20px" }}>
          {!readOnly && (
            <div
              style={{
                marginTop: 2,
                display: "grid",
                gridTemplateColumns: "repeat(2, minmax(140px, 1fr))",
                gap: 8,
                fontSize: 12,
                color: "var(--ink-3)",
              }}
            >
              <div>Template: <span style={{ color: "var(--ink)" }}>{tool.template || "–"}</span></div>
              <div>Primitive: <span style={{ color: "var(--ink)" }}>{tool.primitive || "–"}</span></div>
              <div>Usage: <span style={{ color: "var(--ink)" }}>{tool.metrics?.usageCount || 0}</span></div>
              <div>Success: <span style={{ color: "var(--ink)" }}>{successRate == null ? "–" : `${successRate}%`}</span></div>
            </div>
          )}

          <div
            style={{
              marginTop: 16,
              fontSize: 12,
              color: "var(--ink-2)",
              fontWeight: 600,
              textTransform: "uppercase",
              letterSpacing: 0.5,
            }}
          >
            How The AI Calls This Tool
          </div>
          <div style={{ marginTop: 8, fontSize: 12, color: "var(--ink-3)", lineHeight: 1.5 }}>
            Use <span style={{ color: "var(--blue)", fontFamily: "monospace" }}>{tool.mcpName}</span> with an arguments object.
          </div>
          <div style={{ marginTop: 10, fontSize: 12, color: "var(--ink-3)" }}>
            Required arguments:{" "}
            <span style={{ color: "var(--ink)" }}>
              {requiredKeys.length > 0 ? requiredKeys.join(", ") : "none"}
            </span>
          </div>
          <div style={{ marginTop: 10, fontSize: 12, color: "var(--ink-3)" }}>Input schema</div>
          <pre
            style={{
              margin: "6px 0 0",
              background: "var(--panel)",
              border: "1px solid var(--line)",
              borderRadius: 8,
              padding: 12,
              fontSize: 12,
              color: "var(--ink)",
              lineHeight: 1.45,
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              fontFamily: "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, monospace",
            }}
          >
            {JSON.stringify(schema, null, 2)}
          </pre>
        </div>

        {!readOnly && (
          <div
            style={{
              padding: "12px 20px",
              borderTop: "1px solid var(--line)",
              display: "flex",
              gap: 8,
              justifyContent: "flex-end",
            }}
          >
            <button
              onClick={onToggleEnabled}
              disabled={busy}
              style={{
                padding: "6px 14px",
                fontSize: 12,
                borderRadius: 6,
                border: "1px solid var(--line)",
                background: tool.enabled ? "#e6e9ef" : "var(--blue-soft)",
                color: tool.enabled ? "var(--ink-2)" : "var(--blue)",
                cursor: busy ? "wait" : "pointer",
              }}
            >
              {busy ? "Working…" : tool.enabled ? "Disable" : "Enable"}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function ToolsPage({ newlyAddedToolIds = [], onMarkToolSeen }) {
  const [tools, setTools] = useState([]);
  const [skillDetails, setSkillDetails] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busyToolId, setBusyToolId] = useState(null);
  const [deleteToolId, setDeleteToolId] = useState(null);
  const [previewTool, setPreviewTool] = useState(null);
  const [previewStaticTool, setPreviewStaticTool] = useState(null);
  const [previewSkill, setPreviewSkill] = useState(null);
  const [search, setSearch] = useState("");
  const [error, setError] = useState("");

  const reload = useCallback(async () => {
    try {
      setError("");
      const [toolData, skillsMeta] = await Promise.all([fetchTools(), fetchSkills()]);
      const candidateSkills = Array.isArray(skillsMeta)
        ? skillsMeta.filter((s) => s && s.id && (s.source === "custom" || (s.tags || []).includes("learned")))
        : [];
      const detailedSkills = await Promise.all(
        candidateSkills.map(async (s) => {
          try {
            return await fetchSkillDetails(s.id);
          } catch {
            return s;
          }
        })
      );
      setTools(Array.isArray(toolData) ? toolData : []);
      setSkillDetails(detailedSkills.filter(Boolean));
    } catch {
      setError("Failed to load learned tools");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    reload();
    const timer = setInterval(reload, 60000);
    return () => clearInterval(timer);
  }, [reload]);

  useEffect(() => {
    if (!previewTool) return;
    const next = tools.find((t) => t.id === previewTool.id) || null;
    if (!next) {
      setPreviewTool(null);
      return;
    }
    if (next !== previewTool) setPreviewTool(next);
  }, [tools, previewTool]);

  const filteredTools = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return tools;
    return tools.filter((tool) => {
      const haystack = [
        tool.id,
        tool.name,
        tool.description,
        tool.mcpName,
        tool.template,
        tool.primitive,
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [tools, search]);

  const newlyAddedToolIdSet = useMemo(
    () =>
      new Set(
        (Array.isArray(newlyAddedToolIds) ? newlyAddedToolIds : [])
          .map((id) => String(id || "").trim())
          .filter(Boolean)
      ),
    [newlyAddedToolIds]
  );

  const relatedSkillsByToolId = useMemo(() => {
    const map = new Map();
    const add = (toolId, skill) => {
      if (!toolId || !skill || !skill.id || !skill.name) return;
      const key = String(toolId).trim();
      if (!key) return;
      if (!map.has(key)) map.set(key, []);
      const arr = map.get(key);
      if (!arr.some((s) => s.id === skill.id)) arr.push(skill);
    };

    const extractToolIds = (text) => {
      const out = [];
      const re = /learned__([a-z0-9_]+)/gi;
      const src = String(text || "");
      let m;
      while ((m = re.exec(src)) !== null) out.push(m[1]);
      return out;
    };

    for (const skill of skillDetails) {
      const skillId = String(skill?.id || "").trim();
      const skillName = String(skill?.name || skill?.id || "").trim();
      if (!skillId || !skillName) continue;
      const skillRef = { id: skillId, name: skillName };
      const references = new Set([
        ...extractToolIds(skill?.content),
        ...extractToolIds(skill?.description),
        ...((Array.isArray(skill?.dependencies) ? skill.dependencies : [])
          .map((d) => String(d || "").trim())
          .filter((d) => d.startsWith("learned__"))
          .map((d) => d.slice("learned__".length))),
      ]);
      for (const toolId of references) add(toolId, skillRef);
    }
    return map;
  }, [skillDetails]);

  const openRelatedSkill = async (skillId) => {
    if (!skillId) return;
    const existing = skillDetails.find((s) => s.id === skillId);
    if (existing && existing.content) {
      setPreviewSkill(existing);
      return;
    }
    try {
      const detail = await fetchSkillDetails(skillId);
      if (detail) setPreviewSkill(detail);
    } catch {}
  };

  const toggleEnabled = async (tool) => {
    setBusyToolId(tool.id);
    try {
      await updateToolProcedure(tool.id, { enabled: !tool.enabled });
      await reload();
    } catch {
      setError("Failed to update tool");
    } finally {
      setBusyToolId(null);
    }
  };

  const deleteTool = async (toolId) => {
    setBusyToolId(toolId);
    try {
      await deleteToolProcedure(toolId);
      await reload();
      setDeleteToolId(null);
      if (previewTool?.id === toolId) setPreviewTool(null);
    } catch {
      setError("Failed to delete tool");
    } finally {
      setBusyToolId(null);
    }
  };

  return (
    <div
      style={{
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: "var(--bg)",
      }}
    >
      <div
        style={{
          padding: "16px 24px",
          borderBottom: "1px solid var(--line)",
          display: "flex",
          alignItems: "center",
          gap: 12,
        }}
      >
        <span style={{ fontSize: 24, display: "inline-flex", alignItems: "center" }}>{ICONS.wrench(24)}</span>
        <div>
          <div style={{ fontSize: 18, fontWeight: 700, color: "var(--ink)" }}>Tools</div>
          <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
            Static MCP tools for reference + learned tools you can manage
          </div>
        </div>
      </div>

      {error && (
        <div
          style={{
            padding: "8px 24px",
            borderBottom: "1px solid var(--line)",
            color: "var(--red)",
            fontSize: 12,
          }}
        >
          {error}
        </div>
      )}

      <div style={{ flex: 1, overflow: "auto", padding: 20 }}>
        <div
          style={{
            border: "1px solid var(--line)",
            borderRadius: 10,
            marginBottom: 16,
            overflow: "hidden",
            background: "var(--bg)",
          }}
        >
          <div
            style={{
              padding: "10px 12px",
              borderBottom: "1px solid var(--line)",
              display: "flex",
              alignItems: "center",
              gap: 8,
              background: "var(--panel)",
            }}
          >
            <span style={{ fontSize: 14, display: "inline-flex", alignItems: "center" }}>{ICONS.book(14)}</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "var(--ink)" }}>
              Static MCP Tools (Reference)
            </span>
            <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-3)" }}>
              {STATIC_MCP_TOOL_DEFS.length} tools
            </span>
          </div>
          <div
            style={{
              padding: 12,
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))",
              gap: 10,
            }}
          >
            {STATIC_MCP_TOOL_DEFS.map((tool) => (
              <div key={tool.id} onClick={() => setPreviewStaticTool(tool)}>
                <StaticToolRefCard tool={tool} />
              </div>
            ))}
          </div>
        </div>

        <div
          style={{
            border: "1px solid var(--line)",
            borderRadius: 10,
            overflow: "hidden",
            background: "var(--bg)",
          }}
        >
          <div
            style={{
              padding: "10px 12px",
              borderBottom: "1px solid var(--line)",
              display: "flex",
              alignItems: "center",
              gap: 8,
              background: "var(--panel)",
            }}
          >
            <span style={{ fontSize: 14, display: "inline-flex", alignItems: "center" }}>{ICONS.brain(14)}</span>
            <span style={{ fontSize: 13, fontWeight: 700, color: "var(--ink)" }}>
              Dynamic Learned Tools
            </span>
            <span style={{ marginLeft: "auto", fontSize: 12, color: "var(--ink-3)" }}>
              {filteredTools.length} tool{filteredTools.length !== 1 ? "s" : ""}
            </span>
          </div>
          <div
            style={{
              padding: 12,
              borderBottom: "1px solid var(--line)",
              display: "flex",
              gap: 10,
              alignItems: "center",
            }}
          >
            <input
              type="text"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              placeholder="Search learned tools by name, template, primitive, or description..."
              style={{
                flex: 1,
                padding: "7px 12px",
                fontSize: 13,
                background: "var(--panel)",
                border: "1px solid var(--line)",
                borderRadius: 6,
                color: "var(--ink)",
                outline: "none",
              }}
            />
          </div>
          <div style={{ padding: 12 }}>
            {loading ? (
              <div style={{ color: "var(--ink-3)", padding: 20 }}>Loading…</div>
            ) : filteredTools.length === 0 ? (
              <div style={{ textAlign: "center", padding: 40 }}>
                <div style={{ fontSize: 32, marginBottom: 10 }}>{ICONS.wrench(32)}</div>
                <div style={{ fontSize: 15, color: "var(--ink)", fontWeight: 600, marginBottom: 6 }}>
                  No learned tools found
                </div>
                <div style={{ fontSize: 12, color: "var(--ink-3)" }}>
                  {search.trim()
                    ? "Try a different search term."
                    : "No learned tools have been promoted yet."}
                </div>
              </div>
            ) : (
              <div
                style={{
                  display: "grid",
                  gridTemplateColumns: "repeat(auto-fill, minmax(300px, 1fr))",
                  gap: 16,
                }}
              >
                {filteredTools.map((tool) => (
                  <ToolPageCard
                    key={tool.id}
                    tool={tool}
                    busy={busyToolId === tool.id}
                    isNew={newlyAddedToolIdSet.has(String(tool?.id || "").trim())}
                    onClick={() => {
                      if (typeof onMarkToolSeen === "function") {
                        onMarkToolSeen(String(tool?.id || "").trim());
                      }
                      setPreviewTool(tool);
                    }}
                    onToggleEnabled={() => toggleEnabled(tool)}
                    onDelete={() => setDeleteToolId(tool.id)}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
      {previewTool && (
        <ToolDetailModal
          tool={previewTool}
          relatedSkills={relatedSkillsByToolId.get(previewTool.id) || []}
          busy={busyToolId === previewTool.id}
          onClose={() => setPreviewTool(null)}
          onToggleEnabled={() => toggleEnabled(previewTool)}
          onDelete={() => setDeleteToolId(previewTool.id)}
          onOpenSkill={openRelatedSkill}
        />
      )}
      {previewStaticTool && (
        <ToolDetailModal
          tool={previewStaticTool}
          relatedSkills={[]}
          busy={false}
          onClose={() => setPreviewStaticTool(null)}
          onToggleEnabled={() => {}}
          onDelete={() => {}}
          onOpenSkill={() => {}}
          readOnly={true}
        />
      )}
      {previewSkill && (
        <SkillPageDetailModal
          skill={previewSkill}
          onClose={() => setPreviewSkill(null)}
        />
      )}
      {deleteToolId && (
        <ConfirmDeleteModal
          message={`Delete learned tool "${deleteToolId}"? This cannot be undone.`}
          onConfirm={() => deleteTool(deleteToolId)}
          onCancel={() => setDeleteToolId(null)}
        />
      )}
    </div>
  );
}

// ─── StatusDot ───────────────────────────────────────────────────────────────

function StatusDot({ label, active, activeColor, inactiveColor }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13, fontWeight: 500, color: "var(--ink-3)" }}>
      <div
        style={{
          width: 8,
          height: 8,
          borderRadius: "50%",
          flexShrink: 0,
          background: active ? activeColor : inactiveColor,
          boxShadow: active ? `0 0 5px ${activeColor}` : "none",
        }}
      />
      {label}
    </div>
  );
}

function ArtifactToastStack({ items }) {
  if (!Array.isArray(items) || items.length === 0) return null;

  return (
    <div
      style={{
        position: "fixed",
        bottom: 20,
        right: 20,
        zIndex: 2000,
        width: 300,
        display: "flex",
        flexDirection: "column",
        gap: 8,
        pointerEvents: "none",
      }}
    >
      {items.map((item) => {
        const isTool = item.kind === "tool";
        const accent = isTool ? "#dc2626" : "#16a34a";
        const title = isTool ? "New Tool Learned" : "New Skill Learned";
        return (
          <div
            key={item.id}
            style={{
              minHeight: 40,
              borderRadius: 6,
              border: `1px solid ${accent}44`,
              borderLeft: `4px solid ${accent}`,
              background: "linear-gradient(90deg, var(--panel) 0%, var(--bg) 100%)",
              padding: "6px 10px",
              display: "flex",
              alignItems: "center",
              gap: 8,
              boxShadow: "0 8px 24px rgba(0,0,0,0.35)",
            }}
          >
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  fontSize: 12,
                  letterSpacing: 0.2,
                  textTransform: "uppercase",
                  color: accent,
                  lineHeight: 1.2,
                  fontWeight: 700,
                }}
              >
                {title}
              </div>
              <div
                style={{
                  marginTop: 2,
                  fontSize: 12,
                  color: "var(--ink)",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                  lineHeight: 1.2,
                }}
                title={item.name}
              >
                {item.name}
              </div>
            </div>
            {item.extraCount > 0 && (
              <div
                style={{
                  flexShrink: 0,
                  fontSize: 12,
                  color: "var(--ink-3)",
                  background: "var(--panel-2)",
                  border: "1px solid var(--line)",
                  borderRadius: 999,
                  padding: "2px 8px",
                }}
              >
                +{item.extraCount}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─── useSession — slot acquire, 60s heartbeat, 30-min countdown ──────────────
// Design rules:
//  - "dev" mode: server returns {dev:true} → no countdown, no modals, full access
//  - "managed" mode: server returns real token+TTL → countdown + expiry modal
//  - Pool full: server returns {error,code:"POOL_FULL"} → waiting room modal
//  - Never store _dev tokens in sessionStorage (they don't survive server restart)

const STORAGE_KEY   = "sw_session_token";
const HEARTBEAT_MS  = 60_000;
const WARN_SECS     = 5 * 60;   // warn when < 5 min left

function useSession() {
  const [session,  setSession]  = useState(null);   // null=loading, {dev,token,...}=ready
  const [poolFull, setPoolFull] = useState(null);   // {message, queueLength} | null
  const [secsLeft, setSecsLeft] = useState(null);
  const [expired,  setExpired]  = useState(false);
  const acquiredAt = useRef(null);
  const ttlMsRef   = useRef(0);

  // ── Acquire on mount ───────────────────────────────────────────────────────
  useEffect(() => {
    let cancelled = false;

    async function init() {
      // Try to reuse a real saved token (never reuse "_dev")
      const saved = sessionStorage.getItem(STORAGE_KEY);
      if (saved && saved !== "_dev") {
        try {
          const r = await fetch(`${API_BASE}/session/heartbeat`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "x-session-token": saved },
            body: "{}",
          });
          const d = await r.json();
          if (d.ok && !cancelled) {
            acquiredAt.current = Date.now() - (d.idleMs || 0);
            setSession({ token: saved, dev: false });
            return; // reuse succeeded
          }
        } catch {}
        // Saved token invalid — clear and acquire fresh
        sessionStorage.removeItem(STORAGE_KEY);
      }

      // Acquire a new slot
      try {
        const r = await fetch(`${API_BASE}/session/acquire`, { method: "POST" });
        const d = await r.json();
        if (cancelled) return;

        if (d.code === "POOL_FULL" || (d.error && !d.token)) {
          setPoolFull({ message: d.error || "Server at capacity", queueLength: d.queueLength });
          return;
        }

        if (d.dev) {
          // Server is in single-user dev mode — no session management
          setSession({ token: "_dev", dev: true });
          return;
        }

        // Real managed session
        sessionStorage.setItem(STORAGE_KEY, d.token);
        acquiredAt.current = Date.now();
        ttlMsRef.current   = d.sessionTtlMs || 30 * 60 * 1000;
        setSession(d);
      } catch {
        // Server not reachable — run in offline/dev mode, no modals
        if (!cancelled) setSession({ token: "_dev", dev: true });
      }
    }

    init();
    return () => { cancelled = true; };
  }, []);

  // ── Heartbeat (managed sessions only) ─────────────────────────────────────
  useEffect(() => {
    if (!session || session.dev) return;
    const iv = setInterval(async () => {
      try {
        const r = await fetch(`${API_BASE}/session/heartbeat`, {
          method: "POST",
          headers: { "Content-Type": "application/json", "x-session-token": session.token },
          body: "{}",
        });
        const d = await r.json();
        if (!d.ok) setExpired(true);
      } catch {}
    }, HEARTBEAT_MS);
    return () => clearInterval(iv);
  }, [session]);

  // ── Countdown (managed sessions only) ─────────────────────────────────────
  useEffect(() => {
    if (!session || session.dev || !acquiredAt.current || !ttlMsRef.current) return;
    const iv = setInterval(() => {
      const left = Math.max(0, ttlMsRef.current - (Date.now() - acquiredAt.current));
      setSecsLeft(Math.floor(left / 1000));
      if (left === 0) { setExpired(true); clearInterval(iv); }
    }, 1000);
    return () => clearInterval(iv);
  }, [session]);

  // ── Release on unload (managed sessions only) ──────────────────────────────
  useEffect(() => {
    if (!session || session.dev) return;
    const handler = () => {
      navigator.sendBeacon?.(`${API_BASE}/session/release`, JSON.stringify({ token: session.token }));
      sessionStorage.removeItem(STORAGE_KEY);
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [session]);

  return {
    session,
    poolFull,
    secsLeft,
    expired,
    isLoading:   session === null && !poolFull,
    warningSoon: secsLeft !== null && secsLeft < WARN_SECS,
  };
}

// ─── Settings Modal ──────────────────────────────────────────────────────────

function SettingsModal({ uiTheme, onThemeChange, layoutMode, onLayoutMode, onClose }) {
  const themes = [
    {
      id: "dark", label: "Dark", desc: "Paper UI — default",
      preview: { nav: "#171b21", bg: "#111418", left: "#1b2028", center: "#090b10", right: "#1b2028", border: "#323946", radius: 8 },
    },
    {
      id: "light", label: "Light", desc: "Clean & bright",
      preview: { nav: "#fff", bg: "#f4f6fa", left: "#fff", center: "#0b1220", right: "#fff", border: "#e6e9ef", radius: 8 },
    },
  ];
  const layouts = [
    { id: "scene",    label: "Scene Generation",  desc: "Intent+SimCoder | Viewport | Scene Inspector", left: true,  right: true  },
    { id: "task",     label: "Task Generation",   desc: "Task Builder | Viewport | Task Inspector",     left: true,  right: true  },
    { id: "training", label: "Agent Training",    desc: "Training Config | Viewport | Agent Monitor",   left: true,  right: true  },
    { id: "coevolve", label: "Co-evolution",      desc: "Curriculum Builder | Viewport | Round Inspector", left: true, right: true },
  ];

  return (
    <div
      style={{ position:"fixed", inset:0, background:"rgba(0,0,0,0.55)",
        display:"flex", alignItems:"center", justifyContent:"center",
        zIndex:9800, backdropFilter:"blur(4px)" }}
      onClick={e => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div style={{ background:"var(--panel)", border:"1px solid var(--line)",
        borderRadius:12, padding:"28px 32px", width:560, maxWidth:"92vw",
        boxShadow:"var(--shadow-pop)", maxHeight:"90vh", overflowY:"auto" }}>

        {/* Header */}
        <div style={{ display:"flex", alignItems:"center", justifyContent:"space-between", marginBottom:24 }}>
          <span style={{ fontSize:18, fontWeight:700, color:"var(--ink)", letterSpacing:"-0.01em" }}>Settings</span>
          <button onClick={onClose} style={{ width:30, height:30, borderRadius:6, border:"none",
            background:"transparent", cursor:"pointer", color:"var(--ink-3)",
            display:"flex", alignItems:"center", justifyContent:"center" }}
            onMouseEnter={e=>e.currentTarget.style.background="var(--bg-hover)"}
            onMouseLeave={e=>e.currentTarget.style.background="transparent"}>
            {ICONS.close(16)}
          </button>
        </div>

        {/* ── Appearance ── */}
        <div style={{ marginBottom:28 }}>
          <div style={{ fontSize:11, fontWeight:700, letterSpacing:"0.07em",
            color:"var(--ink-3)", textTransform:"uppercase", marginBottom:14 }}>
            Appearance
          </div>
          <div style={{ display:"flex", gap:10 }}>
            {themes.map(t => {
              const p = t.preview;
              const active = uiTheme === t.id;
              return (
                <button key={t.id} onClick={() => onThemeChange(t.id)} style={{
                  flex:1, padding:"10px 10px 12px", borderRadius:8, cursor:"pointer",
                  border: active ? "2px solid var(--blue)" : "1px solid var(--line)",
                  background: active ? "var(--blue-soft)" : "var(--bg)",
                  textAlign:"center", fontFamily:"inherit", transition:"all 0.12s",
                }}>
                  {/* Mini layout preview */}
                  <div style={{ width:"100%", height:44, borderRadius:p.radius+2, marginBottom:10,
                    overflow:"hidden", border:`1px solid ${p.border}`, background:p.bg,
                    display:"flex", flexDirection:"column" }}>
                    {/* Navbar strip */}
                    <div style={{ height:10, background:p.nav, borderBottom:`1px solid ${p.border}`, flexShrink:0 }}/>
                    {/* 3-col content */}
                    <div style={{ flex:1, display:"flex", gap:1 }}>
                      <div style={{ width:"28%", background:p.left, borderRadius:p.radius, margin:2 }}/>
                      <div style={{ flex:1, background:p.center, borderRadius:p.radius, margin:"2px 0" }}/>
                      <div style={{ width:"28%", background:p.right, borderRadius:p.radius, margin:2 }}/>
                    </div>
                  </div>
                  <div style={{ fontSize:13, fontWeight:600, color:"var(--ink)" }}>{t.label}</div>
                  <div style={{ fontSize:11, color:"var(--ink-3)", marginTop:2 }}>{t.desc}</div>
                </button>
              );
            })}
          </div>
        </div>

        {/* ── Layout Mode ── */}
        <div>
          <div style={{ fontSize:11, fontWeight:700, letterSpacing:"0.07em",
            color:"var(--ink-3)", textTransform:"uppercase", marginBottom:14 }}>
            Layout Mode
          </div>
          <div style={{ display:"flex", flexDirection:"column", gap:6 }}>
            {layouts.map(l => {
              const active = layoutMode === l.id;
              return (
                <button key={l.id} onClick={() => onLayoutMode(l.id)} style={{
                  display:"flex", alignItems:"center", gap:14,
                  padding:"11px 14px", borderRadius:8, cursor:"pointer",
                  border: active ? "2px solid var(--blue)" : "1px solid var(--line)",
                  background: active ? "var(--blue-soft)" : "transparent",
                  textAlign:"left", fontFamily:"inherit", transition:"all 0.12s",
                }}>
                  {/* Mini panel diagram */}
                  <div style={{ display:"flex", gap:3, flexShrink:0 }}>
                    <div style={{ width:14, height:22, borderRadius:3,
                      background: l.left ? "var(--blue)" : "var(--line)",
                      opacity: l.left ? 1 : 0.35,
                    }}/>
                    <div style={{ width:20, height:22, borderRadius:3, background:"var(--blue)" }}/>
                    <div style={{ width:14, height:22, borderRadius:3,
                      background: l.right ? "var(--blue)" : "var(--line)",
                      opacity: l.right ? 1 : 0.35,
                    }}/>
                  </div>
                  <div>
                    <div style={{ fontSize:13, fontWeight:600, color:"var(--ink)" }}>{l.label}</div>
                    <div style={{ fontSize:11, color:"var(--ink-3)", marginTop:1 }}>{l.desc}</div>
                  </div>
                  {active && (
                    <div style={{ marginLeft:"auto", color:"var(--blue)" }}>{ICONS.check(16)}</div>
                  )}
                </button>
              );
            })}
          </div>
        </div>

      </div>
    </div>
  );
}

// ─── App ─────────────────────────────────────────────────────────────────────

function App() {
  // Health comes from SSE StatusContext — no separate /api/health fetch needed
  const statusCtxMain = useStatus();
  const health      = statusCtxMain.health;
  const healthError = !statusCtxMain.health && !statusCtxMain.pieActive; // only show error after SSE connects

  // ── Theme + Studio Mode ───────────────────────────────────────────────────
  const [uiTheme,    setUiTheme]    = useState(() => localStorage.getItem("sw_ui_theme")    || "dark");
  const [studioMode, setStudioMode] = useState(() => localStorage.getItem("sw_studio_mode") || "scene");
  const [topSection, setTopSection] = useState("studio"); // "studio" | "library" | "results"
  const [showSettings, setShowSettings] = useState(false);

  // Artifact chain — tracks what's been produced
  const [artifacts, setArtifacts] = useState({
    scene: null, task: null, training: null, coevolve: null,
  });

  useEffect(() => {
    const themeMap = { dark: "", light: "light" };
    document.documentElement.setAttribute("data-theme", themeMap[uiTheme] ?? "");
    localStorage.setItem("sw_ui_theme", uiTheme);
  }, [uiTheme]);

  useEffect(() => {
    localStorage.setItem("sw_studio_mode", studioMode);
  }, [studioMode]);

  // Column visibility
  const showLeft  = topSection === "studio";
  const showRight = topSection === "studio";

  // One panel per side per mode (clean 1:1 mapping from the plan)
  // Scene     → L: Intent+SimCoder (ChatPanel)     R: Scene Inspector (SceneInspectorPanel)
  // Task      → L: Task Builder (TaskGenPanel)      R: Task Inspector  (TaskInspectorPanel)
  // Training  → L: Training Config (TrainingConfig) R: Agent Monitor   (AgentPanel)
  // Co-evolve → L: Curriculum Builder              R: Round Inspector (RoundInspector)
  const LEFT_PANEL  = { scene:"chat",     task:"taskgen",   training:"trainconfig", coevolve:"curriculum" };
  const RIGHT_PANEL = { scene:"sceneinsp",task:"taskinsp",  training:"agentmonitor",coevolve:"roundinsp"  };
  const leftPanel  = LEFT_PANEL[studioMode]  || "chat";
  const rightPanel2= RIGHT_PANEL[studioMode] || "sceneinsp";

  const [latestScreenshot, setLatestScreenshot] = useState(null);
  const [splitPct, setSplitPct] = useState(38);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [contextRefreshKey, setContextRefreshKey] = useState(0);
  const [dragging, setDragging] = useState(false);
  const [rightPanel, setRightPanel] = useState("viewport");
  // activePage kept for compatibility with drawer/panel refs; main nav uses topSection+studioMode
  const activePage = topSection === "studio" ? "generate" : topSection;
  const [chatRef, setChatRef] = useState(null);
  const [leftTab,    setLeftTab]    = useState("chat");
  const [rightTab,   setRightTab]   = useState("agent");
  const [colLeft,    setColLeft]    = useState(Math.round(window.innerWidth * 0.28));  // ~3/10
  const [colRight,   setColRight]   = useState(Math.round(window.innerWidth * 0.28)); // ~3/10
  const [commHeight,      setCommHeight]      = useState(200); // left bottom (Verifier)
  const [rightBottomH,   setRightBottomH]    = useState(200); // right bottom (Statistics)

  const leftColRef  = useRef(null);
  const rightColRef = useRef(null);

  // Row-resize: snapshot current panel height at mousedown, compute absolute on move
  // Pattern: startPanelH + (startY - currentY) tracks the mouse 1:1
  const makeRowResize = useCallback((currentH, setH) => (e) => {
    e.preventDefault();
    const startY = e.clientY;
    const startPanelH = currentH; // snapshot — does NOT change during drag
    const onMove = (ev) => {
      const newH = startPanelH + (startY - ev.clientY); // up = bigger bottom
      setH(Math.max(80, Math.min(600, newH)));
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, []);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [drawerTab,  setDrawerTab]  = useState("assets");
  const [drawerH,    setDrawerH]    = useState(200);   // px when open
  const colResizingLeft  = useRef(false);
  const colResizingRight = useRef(false);
  const colResizeStart   = useRef({ x:0, colLeft:390, colRight:360 });
  const drawerResizing   = useRef(false);
  const drawerResizeStart = useRef({ y:0, h:200 });
  const layoutRef        = useRef(null);
  const { session, poolFull, secsLeft, expired, isLoading, warningSoon } = useSession();
  const syncStatus = useSync();
  const [artifactUnread, setArtifactUnread] = useState({ skills: false, tools: false });
  const [artifactNewIds, setArtifactNewIds] = useState({ skills: [], tools: [] });
  const [artifactToasts, setArtifactToasts] = useState([]);
  const containerRef = React.useRef(null);
  const knownLearnedSkillIdsRef = useRef(new Set());
  const knownLearnedToolIdsRef = useRef(new Set());
  const artifactBootstrapRef = useRef(false);
  const artifactToastSeqRef = useRef(0);
  const artifactToastTimersRef = useRef(new Map());

  // Fetch stable session ID on mount (health now comes from SSE)
  useEffect(() => {
    fetch(`${API_BASE}/session`).then(r => r.json()).then(d => {
      if (d.sessionId) setCurrentSessionId(d.sessionId);
    }).catch(() => {});
  }, []);

  const dismissArtifactToast = useCallback((id) => {
    if (!id) return;
    setArtifactToasts((prev) => prev.filter((item) => item.id !== id));
    const timerId = artifactToastTimersRef.current.get(id);
    if (timerId) {
      clearTimeout(timerId);
      artifactToastTimersRef.current.delete(id);
    }
  }, []);

  const pushArtifactToast = useCallback(
    (kind, name, extraCount = 0) => {
      const toastId = `artifact-toast-${++artifactToastSeqRef.current}-${Date.now()}`;
      const toast = {
        id: toastId,
        kind: kind === "tool" ? "tool" : "skill",
        name: String(name || "Unnamed"),
        extraCount: Math.max(0, Number(extraCount || 0)),
      };

      setArtifactToasts((prev) => {
        const next = [...prev, toast];
        const overflow = Math.max(0, next.length - EVOLUTION_TOAST_MAX);
        if (overflow > 0) {
          for (const dropped of next.slice(0, overflow)) {
            const tid = artifactToastTimersRef.current.get(dropped.id);
            if (tid) {
              clearTimeout(tid);
              artifactToastTimersRef.current.delete(dropped.id);
            }
          }
        }
        return next.slice(-EVOLUTION_TOAST_MAX);
      });

      const timerId = window.setTimeout(() => {
        dismissArtifactToast(toastId);
      }, EVOLUTION_TOAST_TTL_MS);
      artifactToastTimersRef.current.set(toastId, timerId);
    },
    [dismissArtifactToast]
  );

  useEffect(() => {
    return () => {
      for (const timerId of artifactToastTimersRef.current.values()) {
        clearTimeout(timerId);
      }
      artifactToastTimersRef.current.clear();
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const syncLearnedArtifacts = async () => {
      try {
        const [skillsRaw, toolsRaw] = await Promise.all([fetchSkills(), fetchTools()]);
        if (cancelled) return;

        const learnedSkills = (Array.isArray(skillsRaw) ? skillsRaw : []).filter(isLearnedSkillMeta);
        const learnedTools = Array.isArray(toolsRaw) ? toolsRaw : [];

        const skillsById = new Map();
        for (const skill of learnedSkills) {
          const id = String(skill?.id || "").trim();
          if (!id) continue;
          skillsById.set(id, skill);
        }

        const toolsById = new Map();
        for (const tool of learnedTools) {
          const id = String(tool?.id || "").trim();
          if (!id) continue;
          toolsById.set(id, tool);
        }

        const newlyAddedSkills = [];
        const newlyAddedTools = [];

        for (const [id, skill] of skillsById.entries()) {
          if (!knownLearnedSkillIdsRef.current.has(id)) newlyAddedSkills.push(skill);
        }
        for (const [id, tool] of toolsById.entries()) {
          if (!knownLearnedToolIdsRef.current.has(id)) newlyAddedTools.push(tool);
        }

        knownLearnedSkillIdsRef.current = new Set(skillsById.keys());
        knownLearnedToolIdsRef.current = new Set(toolsById.keys());

        if (!artifactBootstrapRef.current) {
          artifactBootstrapRef.current = true;
          return;
        }

        const newlyAddedSkillIds = newlyAddedSkills
          .map((skill) => String(skill?.id || "").trim())
          .filter(Boolean);
        const newlyAddedToolIds = newlyAddedTools
          .map((tool) => String(tool?.id || "").trim())
          .filter(Boolean);

        setArtifactNewIds((prev) => {
          const nextSkills = (Array.isArray(prev?.skills) ? prev.skills : []).filter((id) =>
            skillsById.has(String(id || "").trim())
          );
          const nextTools = (Array.isArray(prev?.tools) ? prev.tools : []).filter((id) =>
            toolsById.has(String(id || "").trim())
          );

          for (const id of newlyAddedSkillIds) {
            if (!nextSkills.includes(id)) nextSkills.push(id);
          }
          for (const id of newlyAddedToolIds) {
            if (!nextTools.includes(id)) nextTools.push(id);
          }

          const unchanged =
            nextSkills.length === (prev?.skills || []).length &&
            nextTools.length === (prev?.tools || []).length &&
            nextSkills.every((id, idx) => id === (prev?.skills || [])[idx]) &&
            nextTools.every((id, idx) => id === (prev?.tools || [])[idx]);

          return unchanged ? prev : { skills: nextSkills, tools: nextTools };
        });

        if (newlyAddedSkills.length > 0) {
          if (activePage !== "skills") {
            setArtifactUnread((prev) => ({ ...prev, skills: true }));
          }
          const latest = newlyAddedSkills[newlyAddedSkills.length - 1];
          const skillName = String(latest?.name || latest?.id || "Unnamed skill");
          pushArtifactToast("skill", skillName, newlyAddedSkills.length - 1);
        }

        if (newlyAddedTools.length > 0) {
          if (activePage !== "tools") {
            setArtifactUnread((prev) => ({ ...prev, tools: true }));
          }
          const latest = newlyAddedTools[newlyAddedTools.length - 1];
          const toolName = String(latest?.name || latest?.id || "Unnamed tool");
          pushArtifactToast("tool", toolName, newlyAddedTools.length - 1);
        }
      } catch {
        // Ignore transient polling errors.
      }
    };

    // Delay initial sync to avoid connection saturation at startup
    const startTimer = setTimeout(syncLearnedArtifacts, 10000);
    const intervalId = window.setInterval(syncLearnedArtifacts, 60000);
    return () => {
      cancelled = true;
      clearTimeout(startTimer);
      window.clearInterval(intervalId);
    };
  }, [activePage, pushArtifactToast]);

  // Drag handler for split pane
  const handleMouseDown = (e) => {
    e.preventDefault();
    setDragging(true);
  };

  useEffect(() => {
    if (!dragging) return;
    const onMove = (e) => {
      if (!containerRef.current) return;
      const rect = containerRef.current.getBoundingClientRect();
      const pct = ((e.clientX - rect.left) / rect.width) * 100;
      setSplitPct(Math.max(25, Math.min(75, pct)));
    };
    const onUp = () => setDragging(false);
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, [dragging]);

  // ── Column resize drag handlers ────────────────────────────────────────────
  const startColResize = useCallback((side) => (e) => {
    e.preventDefault();
    if (side === "left") {
      colResizingLeft.current = true;
      colResizeStart.current = { x: e.clientX, colLeft, colRight };
    } else {
      colResizingRight.current = true;
      colResizeStart.current = { x: e.clientX, colLeft, colRight };
    }
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    const onMove = (ev) => {
      const dx = ev.clientX - colResizeStart.current.x;
      if (colResizingLeft.current) {
        setColLeft(Math.max(280, Math.min(600, colResizeStart.current.colLeft + dx)));
      } else if (colResizingRight.current) {
        setColRight(Math.max(260, Math.min(560, colResizeStart.current.colRight - dx)));
      }
    };
    const onUp = () => {
      colResizingLeft.current  = false;
      colResizingRight.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup",   onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup",   onUp);
  }, [colLeft, colRight]);

  // ── Drawer resize (drag top edge up/down) ──────────────────────────────────
  const startDrawerResize = useCallback((e) => {
    e.preventDefault();
    drawerResizing.current = true;
    drawerResizeStart.current = { y: e.clientY, h: drawerH };
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
    // Track pending height in a ref — only commit to state on mouseup
    // This prevents iframe resize (and UE data-channel "cannot send yet" logs) on every mousemove
    const pendingH = { value: drawerH };
    const onMove = (ev) => {
      if (!drawerResizing.current) return;
      const delta = drawerResizeStart.current.y - ev.clientY;
      pendingH.value = Math.max(80, Math.min(600, drawerResizeStart.current.h + delta));
      // Update only the drag-handle visual, not the full React state
      const handle = document.querySelector('.sw-drawer .sw-drawer-handle-preview');
      if (handle) handle.style.transform = `translateY(${-delta}px)`;
    };
    const onUp = () => {
      drawerResizing.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      // Commit size only on mouseup — avoids continuous React re-renders + iframe resize
      setDrawerH(pendingH.value);
      if (pendingH.value > 50) setDrawerOpen(true);
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }, [drawerH, drawerOpen]);

  const handleNavClick = useCallback((id) => {
    // kept for compatibility with any remaining callers
    if (id === "generate") setTopSection("studio");
    else if (id === "skills" || id === "tools" || id === "arena") { setTopSection("library"); }
    else if (id === "gallery" || id === "leaderboard") { setTopSection("results"); }
    if (id === "skills") setArtifactUnread(p => p.skills ? { ...p, skills: false } : p);
    if (id === "tools")  setArtifactUnread(p => p.tools  ? { ...p, tools:  false } : p);
  }, []);

  const markSkillArtifactSeen = useCallback((skillId) => {
    const normalizedId = String(skillId || "").trim();
    if (!normalizedId) return;
    setArtifactNewIds((prev) => ({
      ...prev,
      skills: (Array.isArray(prev?.skills) ? prev.skills : []).filter((id) => id !== normalizedId),
    }));
  }, []);

  const markToolArtifactSeen = useCallback((toolId) => {
    const normalizedId = String(toolId || "").trim();
    if (!normalizedId) return;
    setArtifactNewIds((prev) => ({
      ...prev,
      tools: (Array.isArray(prev?.tools) ? prev.tools : []).filter((id) => id !== normalizedId),
    }));
  }, []);

  const activeModeInfo = STUDIO_MODES.find(m => m.id === studioMode) || STUDIO_MODES[0];

  return (
    <PollProvider>
    <div style={{ display:"flex", flexDirection:"column", height:"100vh", background:"var(--bg)", overflow:"hidden", padding:"8px", gap:8, position:"relative" }}>

      {/* ══ POOL FULL — waiting room (only shows when all UE slots are occupied) ══ */}
      {poolFull && (
        <div style={{ position:"fixed", inset:0, background:"var(--bg)", display:"flex", alignItems:"center", justifyContent:"center", zIndex:9999 }}>
          <div style={{ background:"var(--panel)", border:"1px solid var(--line)", borderRadius:16, padding:"40px 48px", textAlign:"center", maxWidth:420, boxShadow:"var(--shadow-pop)" }}>
            <div style={{ width:56, height:56, marginBottom:16, marginLeft:"auto", marginRight:"auto", display:"flex", alignItems:"center", justifyContent:"center", color:"var(--ink-3)" }}>{ICONS.clock(48)}</div>
            <div style={{ fontSize:20, fontWeight:800, color:"var(--ink)", marginBottom:8 }}>Server at capacity</div>
            <div style={{ fontSize:13, color:"var(--ink-3)", lineHeight:1.6, marginBottom:24 }}>
              All simulation slots are currently in use.<br/>
              {poolFull.queueLength > 0 && <>Queue length: <strong style={{color:"var(--blue)"}}>{poolFull.queueLength}</strong><br/></>}
              {poolFull.message}
            </div>
            <button onClick={() => window.location.reload()} className="sw-btn-blue" style={{ width:"100%" }}>
              Try again
            </button>
          </div>
        </div>
      )}

      {/* ══ SESSION EXPIRED modal ══ */}
      {expired && (
        <div style={{ position:"fixed", inset:0, background:"rgba(15,23,42,.6)", display:"flex", alignItems:"center", justifyContent:"center", zIndex:9999, backdropFilter:"blur(4px)" }}>
          <div style={{ background:"var(--panel)", border:"1px solid var(--line)", borderRadius:16, padding:"40px 48px", textAlign:"center", maxWidth:380, boxShadow:"var(--shadow-pop)" }}>
            <div style={{ width:56, height:56, marginBottom:16, marginLeft:"auto", marginRight:"auto", display:"flex", alignItems:"center", justifyContent:"center", color:"var(--ink-3)" }}>{ICONS.lock(48)}</div>
            <div style={{ fontSize:20, fontWeight:800, color:"var(--ink)", marginBottom:8 }}>Session ended</div>
            <div style={{ fontSize:13, color:"var(--ink-3)", lineHeight:1.6, marginBottom:24 }}>
              Your 30-minute session has expired.<br/>Refresh to start a new session.
            </div>
            <button onClick={() => { sessionStorage.removeItem("sw_session_token"); window.location.reload(); }} className="sw-btn-blue" style={{ width:"100%" }}>
              Start new session
            </button>
          </div>
        </div>
      )}

      {/* ══ TOP NAV BAR ══ */}
      <header style={{
        height: 56, background: "var(--topbar,var(--panel))",
        borderRadius: "var(--radius,8px)", border: "1px solid var(--line)",
        boxShadow: "var(--shadow-pop)", display: "flex", alignItems: "center",
        padding: "0 12px", gap: 10, flexShrink: 0, userSelect: "none", zIndex: 20, marginBottom: 8,
      }}>
        {/* Brand */}
        <div className="sw-brand" style={{ paddingRight:12 }}>
          <div style={{ width:34, height:34, borderRadius:"50%", overflow:"hidden", flexShrink:0, boxShadow:"0 0 0 1px #4b5563, 0 4px 12px rgba(0,0,0,0.3)" }}>
            <img src="/simworld-studio-logo.png" style={{ width:"100%", height:"100%", objectFit:"cover" }} alt="SimWorld" />
          </div>
          <span className="sw-brand-name" style={{ fontSize:14 }}>SimWorld Studio</span>
        </div>

        {/* Pipeline stepper — primary nav */}
        {topSection === "studio"
          ? <PipelineStepper activeMode={studioMode} onChange={m => { setStudioMode(m); setTopSection("studio"); }} />
          : <div style={{ flex:1, fontSize:13, fontWeight:700, color:"var(--ink-2)" }}>
              {topSection === "library" ? "Library" : "Results"}
            </div>
        }

        {/* Secondary nav */}
        <div style={{ display:"flex", alignItems:"center", gap:4, flexShrink:0 }}>
          <button className={`sec-nav-btn${topSection==="studio"?"active":""}`}
            onClick={()=>setTopSection("studio")}>{ICONS.layout(13)} Studio</button>
          <button className={`sec-nav-btn${topSection==="library"?"active":""}`}
            onClick={()=>setTopSection("library")}>
            {ICONS.book(13)} Library
            {(artifactUnread.skills || artifactUnread.tools) && <span className="sw-nav-dot" />}
          </button>
          <button className={`sec-nav-btn${topSection==="results"?"active":""}`}
            onClick={()=>setTopSection("results")}>{ICONS.frame(13)} Results</button>
        </div>

        <div style={{ width:1, height:28, background:"var(--line)", flexShrink:0 }} />

        {/* Right side */}
        <div style={{ display:"flex", alignItems:"center", gap:8, justifyContent:"flex-end", marginLeft:"auto" }}>

          {/* Mode-aware primary CTA */}
          {topSection === "studio" && (
            <button className={`primary-cta ${activeModeInfo.ctaColor}`}>
              <span style={{ display:"inline-flex", alignItems:"center" }}>{activeModeInfo.icon(13)}</span>
              {activeModeInfo.cta}
            </button>
          )}

          {/* Status dots */}
          {health && (
            <div style={{ display:"flex", alignItems:"center", gap:14, paddingRight:14, borderRight:"1px solid var(--line-2)" }}>
              <StatusDot label="UE Engine"   active={health.ueConnected}  activeColor="#16a34a" inactiveColor="#dc2626" />
              <StatusDot label="MCP Server"  active={health.mcpConnected} activeColor="#16a34a" inactiveColor="#dc2626" />
              <StatusDot label="LLM" active={true}                activeColor="#16a34a" inactiveColor="#64748b" />
            </div>
          )}
          {!health && <span style={{ fontSize:13, color:"var(--ink-3)" }}>Connecting…</span>}

          {/* Sync error / stale agent warnings */}
          {!syncStatus.sseOk && (
            <div style={{ display:"inline-flex", alignItems:"center", gap:5, padding:"4px 10px", borderRadius:7, background:"var(--error-soft,#fef2f2)", border:"1px solid var(--error-border,#fecaca)", fontSize:13, fontWeight:600, color:"var(--red)" }}>
              {ICONS.warning(14)} {syncStatus.syncError || "SSE disconnected"}
            </div>
          )}
          {syncStatus.staleAgents?.size > 0 && (
            <div title={`Stale: ${[...syncStatus.staleAgents].join(", ")}`}
              style={{ display:"inline-flex", alignItems:"center", gap:5, padding:"4px 10px", borderRadius:7, background:"var(--orange-soft)", border:"1px solid var(--amber)", fontSize:13, fontWeight:600, color:"var(--orange)" }}>
              {ICONS.ghost(14)} {syncStatus.staleAgents.size} stale
            </div>
          )}

          {/* Session countdown — shown when < 5 min remaining */}
          {secsLeft !== null && !session?.dev && (
            <div style={{
              display:"inline-flex", alignItems:"center", gap:6,
              padding:"5px 12px", borderRadius:8,
              background: warningSoon ? "var(--orange-soft,#fff1e6)" : "var(--green-soft,#ecfdf5)",
              border: `1px solid ${warningSoon ? "var(--amber,#f59e0b)" : "var(--green,#16a34a)"}`,
              fontSize:13, fontWeight:700,
              color: warningSoon ? "var(--orange,#ea580c)" : "var(--green,#16a34a)",
            }}>
              {ICONS.clock(14)}
              {Math.floor(secsLeft/60)}:{String(secsLeft%60).padStart(2,"0")}
            </div>
          )}

          {/* Running pill */}
          <div className="sw-running-pill" style={{
            display:"inline-flex", alignItems:"center", gap:8,
            padding:"6px 10px 6px 14px",
            border:"1px solid var(--line)", borderRadius:999,
            background:"var(--panel)", fontSize:13, fontWeight:600,
          }}>
            <span style={{
              width:9, height:9, borderRadius:"50%", background:"#22c55e",
              boxShadow:"0 0 0 3px rgba(34,197,94,.18)", flexShrink:0,
              animation: health?.ueConnected ? "sw-glow-pulse 2s ease-in-out infinite" : "none",
            }}/>
            <span style={{ color:"var(--ink-2)" }}>
              {health?.ueConnected ? "Running" : "Standby"}
            </span>
            {[
              <svg key="play" viewBox="0 0 24 24" width="12" height="12" fill="currentColor" style={{color:"var(--ink-2)"}}><polygon points="5,3 19,12 5,21"/></svg>,
              <svg key="pause" viewBox="0 0 24 24" width="12" height="12" fill="currentColor" style={{color:"var(--ink-2)"}}><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>,
            ].map((icon,i) => (
              <span key={i} style={{
                width:26, height:26, borderRadius:"50%", background:"var(--bg-tertiary,#f1f5f9)",
                display:"flex", alignItems:"center", justifyContent:"center", cursor:"pointer",
              }}>{icon}</span>
            ))}
          </div>

          {/* SimCoder pill */}
          <div className="sw-simcoder-pill" style={{ fontSize:14, padding:"6px 14px 6px 10px" }}>
            <img src="/SimCoder.png" style={{ width:24, height:24, objectFit:"contain", borderRadius:5 }} alt="SimCoder" />
            <span>SimCoder</span>
          </div>

          {/* Avatar */}
          <div style={{
            width:36, height:36, borderRadius:"50%",
            background:"linear-gradient(135deg,#e0e7ff,#c7d2fe)",
            display:"flex", alignItems:"center", justifyContent:"center",
            boxShadow:"0 0 0 2px var(--panel), 0 0 0 3px var(--line)",
            cursor:"pointer", flexShrink:0,
          }}>
            <svg viewBox="0 0 24 24" fill="none" width="20" height="20">
              <circle cx="12" cy="8" r="4" fill="#6366f1"/>
              <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" fill="#6366f1"/>
            </svg>
          </div>

          {/* Settings icon */}
          <button onClick={() => setShowSettings(true)} style={{
            width:36, height:36, borderRadius:8, border:"none", background:"transparent",
            display:"flex", alignItems:"center", justifyContent:"center",
            color:"var(--ink-2)", cursor:"pointer", flexShrink:0,
          }}
            onMouseEnter={e=>e.currentTarget.style.background="var(--bg-hover,#f1f5f9)"}
            onMouseLeave={e=>e.currentTarget.style.background="transparent"}
            title="Settings"
          >
            {ICONS.gear(22)}
          </button>

        </div>
      </header>

      {/* Settings modal */}
      {showSettings && (
        <SettingsModal
          uiTheme={uiTheme}
          onThemeChange={setUiTheme}
          layoutMode={studioMode}
          onLayoutMode={setStudioMode}
          onClose={() => setShowSettings(false)}
        />
      )}

      <ArtifactToastStack items={artifactToasts} />

      {/* ══ ARTIFACT CHAIN ══ */}
      {topSection === "studio" && (
        <ArtifactChain
          artifacts={artifacts}
          activeMode={studioMode}
          onSelect={m => setStudioMode(m)}
        />
      )}

      {/* ══ 3-COLUMN RESIZABLE STUDIO LAYOUT ══ */}
      {topSection === "studio" && (
      <div ref={layoutRef} style={{
        flex: 1, display:"flex", overflow:"hidden", minHeight:0, gap:5, padding:"4px 0",
      }}>
        {/* ── LEFT PANEL ── */}
        {showLeft && <div ref={leftColRef} style={{
          width: colLeft, minWidth:260, maxWidth:640, flexShrink:0,
          display:"flex", flexDirection:"column", gap:0, overflow:"visible", padding:"0 4px", margin:"0 -4px",
        }}>
          {/* ── Left panel content — driven by studioMode ── */}
          <div className="sw-panel-card" style={{
            flex:1, minHeight:0, borderRadius:12, border:"1px solid var(--line)",
            display:"flex", flexDirection:"column", overflow:"hidden", background:"var(--panel)",
          }}>
            <div className="sw-panel-header">
              <span className="sw-section-title">
                <span className="sw-num-chip" style={{ background:"var(--ink-3)" }}>
                  {leftPanel === "chat"       ? ICONS.chat(11)
                  : leftPanel === "taskgen"   ? ICONS.target(11)
                  : leftPanel === "trainconfig"? ICONS.activity(11)
                  :                             ICONS.refresh(11)}
                </span>
                {leftPanel === "chat"        ? "Intent + SimCoder"
                : leftPanel === "taskgen"    ? "Task Builder"
                : leftPanel === "trainconfig"? "Training Config"
                :                              "Curriculum Builder"}
              </span>
            </div>
            <div style={{ flex:1, overflow:"hidden", display:"flex", flexDirection:"column", minHeight:0 }}>
              {leftPanel === "chat" && (
                <ChatPanel
                  onScreenshotUpdate={url => setLatestScreenshot(url)}
                  onRef={setChatRef}
                  onSessionChange={setCurrentSessionId}
                  onChatDone={() => setContextRefreshKey(k => k + 1)}
                />
              )}
              {leftPanel === "taskgen"    && <TaskGenPanel sessionId={currentSessionId} />}
              {leftPanel === "trainconfig"&& <TrainingConfigPanel sessionId={currentSessionId} />}
              {leftPanel === "curriculum" && <CurriculumBuilderPanel sessionId={currentSessionId} />}
            </div>
          </div>
        </div>}

        {/* ── Resize handle left ── */}
        {showLeft && <div className="sw-resize-col" onMouseDown={startColResize("left")} />}

        {/* ── CENTER: Viewport + drawer ── */}
        <div style={{ flex:1, minWidth:320, display:"flex", flexDirection:"column", gap:8, overflow:"visible", padding:"0 3px", margin:"0 -3px" }}>

          {/* UE Viewport card */}
          <div className="sw-panel-card" style={{
            flex:1, minHeight:120,
            borderRadius:12, border:"1px solid #1e293b",
            overflow:"hidden", background:"#0b1220",
          }}>
            <ViewportPanel latestScreenshot={latestScreenshot} />
          </div>

          {/* Drawer: Assets / Scenes / Context / Tools */}
          <div className={`sw-drawer${drawerOpen ? "" : " collapsed"}`}
            style={{ height: drawerOpen ? drawerH : 36 }}>
            {/* Drawer drag handle — drag up/down to resize */}
            <div
              onMouseDown={startDrawerResize}
              style={{
                height:6, background:"transparent", cursor:"row-resize", flexShrink:0,
                display:"flex", alignItems:"center", justifyContent:"center",
              }}
              title="Drag to resize drawer"
            >
              <div style={{ width:32, height:2, borderRadius:2, background:"var(--line)", transition:"background .15s" }}
                onMouseEnter={e => e.currentTarget.style.background = "var(--blue)"}
                onMouseLeave={e => e.currentTarget.style.background = "var(--line)"}
              />
            </div>
            {/* Drawer header — always visible */}
            <div className="sw-drawer-header" onClick={() => setDrawerOpen(o => !o)}>
              <span style={{ fontSize:11, color:"var(--ink-3)", marginRight:4 }}>
                {drawerOpen ? ICONS.chevronDown?.(10) : ICONS.folder(10)}
              </span>
              <span style={{ fontSize:12, fontWeight:700, color:"var(--ink-2)" }}>
                {drawerOpen ? drawerTab.charAt(0).toUpperCase()+drawerTab.slice(1) : "Build Timeline — Assets / Scenes / Context"}
              </span>
              <div style={{ flex:1 }} />
              {drawerOpen && (
                <div style={{ display:"flex", gap:2 }}>
                  {(studioMode === "scene"
                    ? [{ id:"assets",  label:"Assets" },{ id:"scenes",  label:"Scene Versions" },{ id:"context", label:"Tool Calls" }]
                    : studioMode === "task"
                    ? [{ id:"assets",  label:"Task Sets" },{ id:"scenes",  label:"Episodes" },{ id:"context", label:"Validation" }]
                    : studioMode === "training"
                    ? [{ id:"assets",  label:"Episodes" },{ id:"scenes",  label:"Trajectories" },{ id:"context", label:"Metrics" }]
                    : [{ id:"assets",  label:"Rounds" },{ id:"scenes",  label:"Difficulty" },{ id:"context", label:"Rules" }]
                  ).map(t => (
                    <button key={t.id}
                      className={`sw-tab-btn${drawerTab===t.id?" active":""}`}
                      onClick={e => { e.stopPropagation(); setDrawerTab(t.id); }}
                      style={{ fontSize:11, padding:"2px 8px" }}
                    >{t.label}</button>
                  ))}
                  {/* Height adjusters */}
                  {[150,220,320].map(h => (
                    <button key={h} onClick={e=>{ e.stopPropagation(); setDrawerH(h); }}
                      style={{ fontSize:12, padding:"2px 6px", borderRadius:5, border:"1px solid var(--line)",
                        background: drawerH===h?"var(--blue-soft)":"transparent",
                        color: drawerH===h?"var(--blue)":"var(--ink-3)", cursor:"pointer" }}>
                      {h === 150 ? "S" : h === 220 ? "M" : "L"}
                    </button>
                  ))}
                </div>
              )}
            </div>
            {/* Drawer body */}
            {drawerOpen && (
              <div className="sw-drawer-body">
                {drawerTab === "assets" && (
                  <AssetBrowser onInsert={path => chatRef?.insertText(`Use Asset: ${path}`)} />
                )}
                {drawerTab === "scenes" && (
                  <SceneManager onLoadScene={scene => chatRef?.loadScene(scene)} currentSessionId={currentSessionId} />
                )}
                {drawerTab === "context" && (
                  <ContextPanel sessionId={currentSessionId} refreshKey={contextRefreshKey} />
                )}
              </div>
            )}
          </div>
        </div>

        {/* ── Resize handle right ── */}
        {showRight && <div className="sw-resize-col" onMouseDown={startColResize("right")} />}

        {/* ── RIGHT PANEL — driven by studioMode ── */}
        {showRight && <div ref={rightColRef} style={{
          width: colRight, minWidth:240, maxWidth:560, flexShrink:0,
          display:"flex", flexDirection:"column", gap:0, overflow:"visible", padding:"0 4px", margin:"0 -4px",
        }}>
          <div className="sw-panel-card" style={{
            flex:1, minHeight:0, borderRadius:12, border:"1px solid var(--line)",
            display:"flex", flexDirection:"column", overflow:"hidden", background:"var(--panel)",
          }}>
            <div className="sw-panel-header">
              <span className="sw-section-title">
                <span className="sw-num-chip" style={{ background:"var(--ink-3)" }}>
                  {rightPanel2 === "sceneinsp"    ? ICONS.scan(11)
                  : rightPanel2 === "taskinsp"    ? ICONS.check(11)
                  : rightPanel2 === "agentmonitor"? ICONS.robot(11)
                  :                                 ICONS.chartBar(11)}
                </span>
                {rightPanel2 === "sceneinsp"    ? "Scene Inspector"
                : rightPanel2 === "taskinsp"    ? "Task Inspector"
                : rightPanel2 === "agentmonitor"? "Agent Monitor"
                :                                 "Round Inspector"}
              </span>
            </div>
            <div style={{ flex:1, overflow:"hidden", display:"flex", flexDirection:"column", minHeight:0 }}>
              {rightPanel2 === "sceneinsp"    && <SceneInspectorPanel sessionId={currentSessionId} latestScreenshot={latestScreenshot} />}
              {rightPanel2 === "taskinsp"     && <TaskInspectorPanel />}
              {rightPanel2 === "agentmonitor" && <AgentPanel sessionId={currentSessionId} commHeight={0} onCommHeightChange={() => {}} hideComm />}
              {rightPanel2 === "roundinsp"    && <RoundInspectorPanel sessionId={currentSessionId} />}
            </div>
          </div>
        </div>}

      </div>
      )}

      {/* ══ LIBRARY / RESULTS pages ══ */}
      {(topSection === "library" || topSection === "results") && (
        <div style={{
          flex: 1, overflow: "hidden",
          borderRadius: 12, border: "1px solid var(--line)",
          boxShadow: "var(--shadow-card)", background: "var(--panel)", minHeight: 0,
        }}>
          {topSection === "library" && (
            <LibraryPage
              newlyAddedSkillIds={artifactNewIds.skills}
              onMarkSkillSeen={markSkillArtifactSeen}
              newlyAddedToolIds={artifactNewIds.tools}
              onMarkToolSeen={markToolArtifactSeen}
            />
          )}
          {topSection === "results" && <ResultsPage />}
        </div>
      )}

    </div>
    </PollProvider>
  );
}

export default App;
