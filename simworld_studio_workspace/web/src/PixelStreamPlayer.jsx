import React, { useState, useEffect, useRef, useCallback } from "react";

/**
 * PixelStreamPlayer
 * - Auto-reconnect with exponential backoff
 * - Keepalive via postMessage from iframe (sw-keepalive / sw-stream-connected)
 * - No status badge, no config overlay, no click-to-control gate
 */
export default function PixelStreamPlayer({ playerUrl }) {
  const iframeRef      = useRef(null);
  const reconnectTimer = useRef(null);
  const pingTimer      = useRef(null);
  const lastAlive      = useRef(null);

  const [status,     setStatus]     = useState("idle");
  const [reconnects, setReconnects] = useState(0);

  const effectiveUrl = useCallback(() => playerUrl || null, [playerUrl]);

  // Reset when URL changes
  useEffect(() => {
    if (!playerUrl) return;
    setStatus("connecting");
    setReconnects(0);
  }, [playerUrl]);

  // Listen for messages from iframe
  useEffect(() => {
    function onMsg(e) {
      if (e.data?.type === "sw-stream-connected") {
        setStatus("connected");
        setReconnects(0);
        lastAlive.current = Date.now();
        // Auto-focus so keyboard/mouse work immediately
        setTimeout(() => iframeRef.current?.focus(), 150);
      }
      if (e.data?.type === "sw-keepalive") {
        lastAlive.current = Date.now();
      }
    }
    window.addEventListener("message", onMsg);
    return () => window.removeEventListener("message", onMsg);
  }, []);

  // Fallback: assume connected after 12s if no postMessage (Cirrus direct)
  const connectTimer = useRef(null);
  useEffect(() => {
    if (status !== "connecting") return;
    connectTimer.current = setTimeout(() => {
      setStatus("connected");
      lastAlive.current = Date.now();
    }, 12_000);
    return () => clearTimeout(connectTimer.current);
  }, [status]);

  // Heartbeat — detect silent drops after 60s silence
  useEffect(() => {
    if (status !== "connected") return;
    pingTimer.current = setInterval(() => {
      if (lastAlive.current && Date.now() - lastAlive.current > 60_000) {
        clearInterval(pingTimer.current);
        setStatus("disconnected");
        scheduleReconnect();
      }
    }, 5_000);
    return () => clearInterval(pingTimer.current);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status]);

  const scheduleReconnect = useCallback(() => {
    if (reconnectTimer.current) return;
    const delay = Math.min(2_000 * Math.pow(1.5, reconnects), 30_000);
    setReconnects(n => n + 1);
    reconnectTimer.current = setTimeout(() => {
      reconnectTimer.current = null;
      doReconnect();
    }, delay);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reconnects]);

  const doReconnect = useCallback(() => {
    if (!iframeRef.current) return;
    setStatus("connecting");
    const url = effectiveUrl();
    if (!url) return;
    const sep = url.includes("?") ? "&" : "?";
    iframeRef.current.src = url + sep + "_t=" + Date.now();
  }, [effectiveUrl]);

  const url = effectiveUrl();

  return (
    <div
      style={{ width: "100%", height: "100%", position: "relative", background: "#0b1220" }}
      onClick={() => iframeRef.current?.focus()}
      onMouseEnter={() => iframeRef.current?.focus()}
    >
      {url && (
        <iframe
          ref={iframeRef}
          src={url}
          onLoad={() => {
            if (status === "idle" || status === "disconnected") setStatus("connecting");
          }}
          style={{ width: "100%", height: "100%", border: "none", display: "block" }}
          allow="pointer-lock *; fullscreen *; autoplay *; clipboard-read *; clipboard-write *"
          allowFullScreen
          tabIndex={0}
          title="UE Pixel Streaming"
        />
      )}

      {/* No-URL placeholder */}
      {!url && (
        <div style={{
          position: "absolute", inset: 0, zIndex: 10, pointerEvents: "none",
          display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
          background: "rgba(11,18,32,0.85)", gap: 10,
        }}>
          <div style={{ fontSize: 28, opacity: 0.3 }}>📡</div>
          <div style={{ color: "#64748b", fontSize: 12 }}>No stream URL — is Cirrus running?</div>
        </div>
      )}
    </div>
  );
}
