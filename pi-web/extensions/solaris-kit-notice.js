/**
 * `solaris-kit-notice` — tell a running session that its ground truth moved (#1454).
 *
 * The problem this removes: `pi-web-agent-kit` turns the mounted ServiceBay
 * checkout into Pi's shapes at POD START, and ServiceBay refreshes that checkout
 * hourly. So a corrected recipe reaches the box within the hour and then waits
 * for a restart to reach Pi. Observed 2026-09-20: the fix for a failing build
 * landed in `assists/create-service.md` at 21:52 and the session that was
 * failing on it ran until 22:30 without ever hearing.
 *
 * **Why an extension and not the session daemon.** The ticket says "the sessiond
 * compares" — but the daemon is `@jmfederico/pi-web`, a third-party bundle we do
 * not own and must not patch. Pi's own extension API is the supported seam:
 * `docs/extensions.md` of the pinned `@earendil-works/pi-coding-agent` documents
 * `turn_start` (fired for each turn) and `pi.sendMessage(…, { deliverAs })`,
 * whose `"steer"` mode is "delivered after the current assistant turn finishes
 * executing its tool calls, before the next LLM call". That is exactly the one
 * line on the next turn the ticket asks for, and it is a real message in the
 * session: visible in the transcript, attributed to this extension, and part of
 * the context from then on. The model gate (`pi_model_gate.py`) could have
 * appended the same sentence to the outgoing payload, but that edits a
 * conversation from outside Pi's own model — invisible to the transcript, to
 * Pi's context accounting and to the operator reading back what happened.
 *
 * Nothing is forced: no `triggerTurn`, so an idle session is not woken, and the
 * line says to re-read, it does not re-read for the model.
 */

import { createHash } from "node:crypto";
import { readFileSync, readdirSync } from "node:fs";
import { homedir } from "node:os";
import path from "node:path";

const DEFAULT_KIT = "/opt/servicebay";
// Written by `pi_agent_kit.py` in the same run that generates the skills and
// AGENTS.md — the digest of the kit those copies were made from.
const STAMP_NAME = "servicebay-kit.json";
const STAMP_SOURCES = ["agent-docs", "assists"];
// PI WEB's own precedence, the same order `pi_agent_kit.py` resolves it in.
const AGENT_DIR_ENV = ["PI_WEB_AGENT_DIR", "PI_CODING_AGENT_DIR"];
// A refresh moves one or two files. A first delivery onto a box that had no kit
// at pod start moves all 55, and a notice is a line, not a directory listing.
const MAX_NAMED = 5;

function kitRoot() {
  return (process.env.SERVICEBAY_AGENT_KIT || "").trim() || DEFAULT_KIT;
}

function agentDir() {
  for (const name of AGENT_DIR_ENV) {
    const value = (process.env[name] || "").trim();
    if (value) return value;
  }
  return path.join(homedir(), ".pi", "agent");
}

/** `{path below the mount: sha256}` for every kit file a session's text comes from. */
function kitDigest(root) {
  const digests = {};
  for (const group of STAMP_SOURCES) {
    let names;
    try {
      names = readdirSync(path.join(root, group)).sort();
    } catch {
      continue;
    }
    for (const name of names) {
      try {
        const payload = readFileSync(path.join(root, group, name));
        digests[`${group}/${name}`] = createHash("sha256").update(payload).digest("hex");
      } catch {
        continue;
      }
    }
  }
  return digests;
}

function stampedDigest() {
  try {
    const stamp = JSON.parse(readFileSync(path.join(agentDir(), STAMP_NAME), "utf8"));
    return stamp && typeof stamp.files === "object" && stamp.files !== null ? stamp.files : null;
  } catch {
    return null;
  }
}

function changedFiles(before, after) {
  const names = new Set([...Object.keys(before), ...Object.keys(after)]);
  return [...names].filter((name) => before[name] !== after[name]).sort();
}

function notice(files) {
  const named = files.slice(0, MAX_NAMED).join(", ");
  const rest = files.length > MAX_NAMED ? `, +${files.length - MAX_NAMED} more` : "";
  return `The ServiceBay agent kit changed since this session started (${named}${rest}). Re-read what you are relying on.`;
}

export default function solarisKitNoticeExtension(pi) {
  let baseline = null;

  pi.on("session_start", () => {
    // The stamp rather than a fresh scan: the skills and AGENTS.md this session
    // carries were generated at pod start, so pod start is what "since this
    // session started" means here — including for a session opened an hour later
    // whose ground truth is already behind the mount.
    baseline = stampedDigest();
  });

  pi.on("turn_start", () => {
    if (baseline === null) return;
    const current = kitDigest(kitRoot());
    // An unreadable mount is not 55 retired assists; without this it would be
    // reported as one and would replace the baseline with an empty one.
    if (Object.keys(current).length === 0) return;
    const files = changedFiles(baseline, current);
    if (files.length === 0) return;
    // Said once: only a further change earns the next line.
    baseline = current;
    pi.sendMessage(
      {
        customType: "servicebay-kit-notice",
        content: notice(files),
        display: true,
        details: { files },
      },
      { deliverAs: "steer" },
    );
  });
}
