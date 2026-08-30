import React, { useState, useEffect, useRef } from 'react';
import type { 
  PatientState, 
  VitalsInput, 
  PragyanResponse
} from './retriage_timer';
import {
  API,
  CHECK_THRESHOLDS,
  sendVitalsForRetriage
} from './retriage_timer';
import samplePatients from './sample_patients.json';

interface AuditLogEntry {
  timestamp: number;
  patient_id: string;
  old_esi: number;
  new_esi: number;
  response: PragyanResponse;
}

const ESI_COLORS: Record<number, string> = {
  1: '#ff4d4d', // Red
  2: '#ff9933', // Orange
  3: '#ffcc00', // Yellow
  4: '#99cc33', // Light Green
  5: '#339933', // Green
};

const countEvidence = (p: PatientState) =>
  (p.concerns ?? []).reduce((n, c) => n + (c.evidence?.length ?? 0), 0);

/**
 * Where each ESI decision came from: the document, the page number printed on
 * the paper, and the sentence itself, verbatim.
 *
 * The quotes are checked against the source PDFs before they reach here - a
 * citation that could not be verified is marked rather than dropped silently,
 * because "no evidence" and "evidence we could not confirm" are different
 * things and a nurse should be able to tell them apart.
 */
function SourceList({ patient }: { patient: PatientState }) {
  const concerns = (patient.concerns ?? []).filter(c => (c.evidence?.length ?? 0) > 0);

  if (!concerns.length) {
    return (
      <div className="sources" style={panelStyle}>
        <em style={{ color: '#8b93a3' }}>
          No protocol citation for this patient. The acuity still stands on the
          NEWS2 score and the nurse's own assessment - there is simply nothing
          in the corpus to quote for it.
        </em>
      </div>
    );
  }

  return (
    <div className="sources" style={panelStyle}>
      {concerns.map((c, ci) => (
        <div key={ci} style={{ marginBottom: '0.6rem' }}>
          <div style={{ color: '#8b93a3', fontSize: '0.75em', marginBottom: '0.25rem' }}>
            {c.clinical_shorthand} &middot; ESI {c.final_esi}
            {c.time_to_treatment_minutes != null && ` · within ${c.time_to_treatment_minutes} min`}
          </div>
          {c.evidence.map((e, ei) => {
            const unverified = (e as any).verified === false;
            return (
              <div key={ei} style={{
                borderLeft: `2px solid ${unverified ? '#fbbf24' : '#4c7dfd'}`,
                padding: '4px 10px', margin: '4px 0', background: '#131721',
                borderRadius: '0 4px 4px 0'
              }}>
                <div style={{ color: unverified ? '#fbbf24' : '#7aa2ff', fontSize: '0.75em' }}>
                  {e.document} — p.{e.page}
                  {unverified && ' · could not be verified in the corpus'}
                </div>
                <div style={{ color: '#c3c9d4', fontSize: '0.8em', fontStyle: 'italic' }}>
                  “{e.criterion}”
                </div>
              </div>
            );
          })}
        </div>
      ))}
    </div>
  );
}

/**
 * Set the ESI by hand.
 *
 * retriage_loop is an escalate-only ratchet: automatic urgency moves down the
 * numbers and never back up, "except by a separate, explicitly logged nurse
 * action that is NOT part of this function". This is that action.
 *
 * De-escalation is the direction that can harm by omission - it moves someone
 * further down the queue - so it asks for a reason and will not submit without
 * one. Escalating needs no justification: the safe direction should be one
 * click, because friction there costs more than it saves.
 */
function OverridePanel({ patient, onClose, onDone }: {
  patient: PatientState;
  onClose: () => void;
  onDone: (esi: number, event: any) => void;
}) {
  const [target, setTarget] = useState<number>(patient.esi);
  const [reason, setReason] = useState('');
  const [by, setBy] = useState('nurse');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const deEscalating = target > patient.esi;
  const blocked = deEscalating && !reason.trim();

  const submit = async () => {
    setBusy(true); setError(null);
    try {
      const res = await fetch(`${API}/api/patients/${encodeURIComponent(patient.patient_id)}/override`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ esi: target, reason: reason.trim(), by: by.trim() || 'nurse' }),
      });
      const body = await res.json();
      if (!res.ok) throw new Error(body?.detail ?? `HTTP ${res.status}`);
      onDone(target, body);
    } catch (e: any) {
      setError(e?.message ?? 'Override failed');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ ...panelStyle, borderColor: '#3d2f10', background: '#161208' }}>
      <div style={{ color: '#fbbf24', fontSize: '0.78em', marginBottom: '0.5rem' }}>
        Manual override — currently ESI {patient.esi}
      </div>

      <div style={{ display: 'flex', gap: '0.3rem', marginBottom: '0.5rem' }}>
        {[1, 2, 3, 4, 5].map(n => (
          <button key={n} onClick={() => setTarget(n)} disabled={busy}
            style={{
              flex: 1, padding: '5px 0', cursor: 'pointer', fontWeight: 600,
              borderRadius: '4px',
              border: n === target ? '2px solid #fff' : '1px solid #3b475f',
              background: n === target ? ESI_COLORS[n] : 'transparent',
              color: n === target ? '#111' : '#8b93a3',
            }}>{n}</button>
        ))}
      </div>

      {deEscalating && (
        <div style={{ color: '#fbbf24', fontSize: '0.75em', marginBottom: '0.35rem' }}>
          This lowers urgency and moves the patient down the queue. A reason is required.
        </div>
      )}

      <input value={by} onChange={e => setBy(e.target.value)} disabled={busy}
        placeholder="your name" style={inputStyle} />
      <input value={reason} onChange={e => setReason(e.target.value)} disabled={busy}
        placeholder={deEscalating ? 'Reason (required)' : 'Reason (optional)'}
        style={inputStyle} />

      {error && <div style={{ color: '#f87171', fontSize: '0.75em' }}>{error}</div>}

      <div style={{ display: 'flex', gap: '0.4rem', marginTop: '0.4rem' }}>
        <button onClick={submit} disabled={busy || blocked || target === patient.esi}
          style={{
            flex: 1, padding: '5px', borderRadius: '4px', border: 'none', fontWeight: 600,
            cursor: (busy || blocked || target === patient.esi) ? 'not-allowed' : 'pointer',
            background: (busy || blocked || target === patient.esi) ? '#2a2f3a' : '#fbbf24',
            color: (busy || blocked || target === patient.esi) ? '#6f7684' : '#111',
          }}>
          {busy ? 'Saving…' : target === patient.esi ? 'Pick a different level'
            : `Set ESI ${target}`}
        </button>
        <button onClick={onClose} disabled={busy}
          style={{ padding: '5px 12px', borderRadius: '4px', cursor: 'pointer',
                   background: 'transparent', border: '1px solid #3b475f', color: '#8b93a3' }}>
          Cancel
        </button>
      </div>
    </div>
  );
}

/**
 * Put the department into surge and show what that changes.
 *
 * Worth watching during a demo, because the interesting part is what does NOT
 * degrade. Under surge the expensive retrieval is rationed by acuity: ESI 4-5
 * are triaged on the deterministic rules and NEWS2 alone, while ESI 1-3 keep
 * their full workup and their citations. Rationing by arrival order instead
 * would make the sick wait behind the well, which is the wrong trade.
 */
function SurgeToggle({ surge, setSurge }: { surge: any; setSurge: (s: any) => void }) {
  useEffect(() => {
    let stop = false;
    const poll = async () => {
      try {
        const res = await fetch(`${API}/api/surge`);
        if (res.ok && !stop) setSurge(await res.json());
      } catch { /* service down; the header already says offline */ }
    };
    poll();
    const t = setInterval(poll, 4000);
    return () => { stop = true; clearInterval(t); };
  }, [setSurge]);

  const on = !!surge?.surging;

  const toggle = async () => {
    try {
      const res = await fetch(`${API}/api/surge`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        // null hands control back to the arrival-rate monitor
        body: JSON.stringify({ forced: on ? null : true }),
      });
      if (res.ok) setSurge(await res.json());
    } catch { /* ignore */ }
  };

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
      <button onClick={toggle} title={surge?.policy ?? 'Toggle surge mode'}
        style={{
          padding: '4px 12px', borderRadius: '4px', fontWeight: 600, cursor: 'pointer',
          border: on ? '1px solid #ff9933' : '1px solid #3b475f',
          background: on ? '#ff9933' : 'transparent',
          color: on ? '#111' : '#8b93a3',
        }}>
        {on ? '● SURGE' : '○ surge'}
      </button>
      {surge && (
        <span style={{ fontSize: '0.7em', color: on ? '#ff9933' : '#6f7684', maxWidth: 300 }}>
          {surge.arrivals_per_min}/min
          {on && ' · ESI 4-5 rationed, ESI 1-3 keep full grounding'}
        </span>
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  width: '100%', marginBottom: '0.3rem', padding: '4px 7px', fontSize: '0.8em',
  background: '#0f1115', border: '1px solid #262c3a', borderRadius: '4px',
  color: '#d7dae0', boxSizing: 'border-box',
};

const panelStyle: React.CSSProperties = {
  marginTop: '0.5rem', padding: '0.6rem', background: '#0f1115',
  border: '1px solid #262c3a', borderRadius: '6px', fontSize: '0.85em'
};

export default function RetriageQueue() {
  const [patients, setPatients] = useState<PatientState[]>([]);
  const [live, setLive] = useState(false);   // true when the service answers
  const [sourcesFor, setSourcesFor] = useState<string | null>(null);
  const [overrideFor, setOverrideFor] = useState<string | null>(null);
  const [surge, setSurge] = useState<any>(null);
  const [logs, setLogs] = useState<AuditLogEntry[]>([]);
  const [simulationMinute, setSimulationMinuteState] = useState(0);
  const simulationMinuteRef = useRef(0);
  
  const setSimulationMinute = (val: number | ((prev: number) => number)) => {
    if (typeof val === 'function') {
      setSimulationMinuteState(prev => {
        const next = val(prev);
        simulationMinuteRef.current = next;
        return next;
      });
    } else {
      simulationMinuteRef.current = val;
      setSimulationMinuteState(val);
    }
  };

  const [speed, setSpeed] = useState<number>(0);
  
  // Vitals entry state
  const [selectedPatientId, setSelectedPatientId] = useState<string | null>(null);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [vitalsForm, setVitalsForm] = useState<VitalsInput>({
    respiratory_rate: undefined,
    spo2: undefined,
    systolic_bp: undefined,
    heart_rate: undefined,
    temperature_c: undefined,
    consciousness: 'A'
  });

  // Load patients from the live service, and keep polling so anyone submitting
  // the intake form appears here without a refresh. Falls back to the bundled
  // sample file if the service is not running, so the demo still opens.
  useEffect(() => {
    const toState = (p: any) => ({
      patient_id: p.patient_id,
      age: p.profile?.age ?? 50,
      esi: p.current_esi_floor ?? p.grounded_esi ?? p.provisional_esi,
      last_check_minute: p.arrival_minute,
      chief_complaint: p.concerns?.[0]?.clinical_shorthand || 'Unknown',
      concerns: p.concerns as any
    });

    let cancelled = false;

    const load = async () => {
      try {
        const res = await fetch(`${API}/api/patients.json`);
        if (!res.ok) throw new Error(String(res.status));
        const feed = await res.json();
        if (cancelled) return;
        setPatients(prev => {
          // Keep whatever the simulation has already escalated locally; only
          // add patients we have not seen. Otherwise a poll would undo the
          // ratchet every few seconds.
          const known = new Set(prev.map(p => p.patient_id));
          const added = feed.filter((p: any) => !known.has(p.patient_id)).map(toState);
          return added.length ? [...prev, ...added] : prev;
        });
        setLive(true);
      } catch {
        if (cancelled) return;
        setLive(false);
        setPatients(prev => prev.length ? prev : samplePatients.map(toState));
      }
    };

    load();
    const poll = setInterval(load, 5000);
    return () => { cancelled = true; clearInterval(poll); };
  }, []);

  // Clock
  useEffect(() => {
    if (speed === 0) return;
    const intervalMs = 1000 / speed; 
    const timer = setInterval(() => {
      setSimulationMinute(prev => prev + 1);
    }, intervalMs);
    return () => clearInterval(timer);
  }, [speed]);

  const handleSubmitVitals = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!selectedPatientId) return;

    // Check if form is completely empty (excluding consciousness which defaults to 'A')
    const hasVitals = vitalsForm.respiratory_rate !== undefined ||
                      vitalsForm.spo2 !== undefined ||
                      vitalsForm.systolic_bp !== undefined ||
                      vitalsForm.heart_rate !== undefined ||
                      vitalsForm.temperature_c !== undefined;

    if (!hasVitals) {
      alert("Please enter at least one vital sign before submitting.");
      return;
    }

    setIsSubmitting(true);
    
    // Fire mock async API call
    const response = await sendVitalsForRetriage(selectedPatientId, vitalsForm, simulationMinute);
    
    setIsSubmitting(false);
    setSelectedPatientId(null);
    setVitalsForm({
      respiratory_rate: undefined,
      spo2: undefined,
      systolic_bp: undefined,
      heart_rate: undefined,
      temperature_c: undefined,
      consciousness: 'A'
    });

    // Update patient with the response
    setPatients(prevPts => prevPts.map(p => {
      if (p.patient_id === response.patient_id) {
        const currentSimMinute = simulationMinuteRef.current;
        
        // Log it
        setLogs(prev => [{
          timestamp: currentSimMinute,
          patient_id: p.patient_id,
          old_esi: p.esi,
          new_esi: response.grounded_esi,
          response: response
        }, ...prev]);

        return {
          ...p,
          esi: response.grounded_esi,
          // Keep the original grounding alongside the re-triage reason.
          // Replacing threw away the citations the admission decision was
          // built on, so Sources showed less the longer a patient waited -
          // exactly backwards. Newest first, history underneath.
          concerns: [...response.concerns, ...p.concerns],
          last_check_minute: currentSimMinute // Reset timer to NOW, not 1.5s ago
        };
      }
      return p;
    }));
  };

  const getTimerState = (patient: PatientState) => {
    const threshold = CHECK_THRESHOLDS[patient.esi] || 0;
    if (threshold === 0) return { due: false, text: 'CONT' };
    
    const elapsed = simulationMinute - patient.last_check_minute;
    const remaining = threshold - elapsed;
    if (remaining <= 0) return { due: true, text: 'CHECK DUE' };
    return { due: false, text: `${remaining}m` };
  };

  return (
    <div className="dashboard">
      <header className="header">
        <h1>
          PatientTriage.ai // Re-Triage Monitor{' '}
          <span style={{ fontSize: '0.6em', color: live ? '#4ade80' : '#ff9933' }}>
            {live ? '● live' : '○ offline — sample data'}
          </span>
        </h1>
        <div className="clock-controls">
          <SurgeToggle surge={surge} setSurge={setSurge} />
          <div className="clock">Sim Time: T+{simulationMinute}m</div>
          <div className="speeds">
            <button className={speed === 0 ? 'active' : ''} onClick={() => setSpeed(0)}>Pause</button>
            <button className={speed === 1 ? 'active' : ''} onClick={() => setSpeed(1)}>1x</button>
            <button className={speed === 5 ? 'active' : ''} onClick={() => setSpeed(5)}>5x</button>
            <button className={speed === 20 ? 'active' : ''} onClick={() => setSpeed(20)}>20x</button>
          </div>
        </div>
      </header>

      <div className="main-content">
        <div className="queue-panel">
          <h2>Waiting Queue ({patients.length})</h2>
          <div className="patient-cards">
            {patients.map(p => {
              const timer = getTimerState(p);
              const cardStyle = timer.due ? { border: '2px solid #ff9933', boxShadow: '0 0 10px #ff9933' } : { border: '1px solid #333' };
              const latestSummary = p.concerns[0]?.nurse_summary || 'No summary available';

              return (
                <div className="patient-card" key={p.patient_id} style={cardStyle}>
                  <div className="card-header">
                    <span className="patient-id">{p.patient_id}</span>
                    <span className="patient-demographic">{p.age}yo</span>
                  </div>
                  <div className="card-body">
                    <div className="esi-badge" style={{ backgroundColor: ESI_COLORS[p.esi] }}>
                      ESI {p.esi}
                    </div>
                    <div className="complaint">{p.chief_complaint}</div>
                  </div>
                  <div className="card-footer">
                    <div className="nurse-summary-preview" style={{ fontSize: '0.85em', color: '#ccc', fontStyle: 'italic', flex: 1, marginRight: '1rem' }}>
                      {latestSummary}
                    </div>
                    <div className={`timer ${timer.due ? 'due-badge' : ''}`} style={timer.due ? { backgroundColor: '#ff9933', color: '#111', padding: '2px 6px', borderRadius: '4px', fontWeight: 'bold' } : {}}>
                      {timer.text}
                    </div>
                  </div>
                  <div className="card-actions" style={{ display: 'flex', alignItems: 'center', gap: '0.5rem' }}>
                    <button
                      onClick={() => setSelectedPatientId(p.patient_id)}
                      style={timer.due ? { backgroundColor: '#ff9933', color: '#111', fontWeight: 'bold' } : {}}
                    >
                      {timer.due ? 'Enter Vitals Now' : 'Update Vitals...'}
                    </button>

                    {/* The citations are the point of the grounding step, and
                        they were invisible here. Small, out of the way, on
                        the right - a nurse opens it to check, not to read. */}
                    {/* Clinical judgement outranks the score. This is the only
                        path allowed to de-escalate - the automatic loop is an
                        escalate-only ratchet by design. */}
                    <button
                      className="override-btn"
                      title="Set the ESI by hand"
                      onClick={() => setOverrideFor(overrideFor === p.patient_id ? null : p.patient_id)}
                      style={{
                        marginLeft: 'auto', fontSize: '0.75em', padding: '3px 9px',
                        background: 'transparent', border: '1px solid #3b475f',
                        color: overrideFor === p.patient_id ? '#fbbf24' : '#8b93a3',
                        borderRadius: '4px', cursor: 'pointer', whiteSpace: 'nowrap'
                      }}
                    >
                      override
                    </button>

                    <button
                      className="sources-btn"
                      title="Show the protocol pages this came from"
                      onClick={() => setSourcesFor(sourcesFor === p.patient_id ? null : p.patient_id)}
                      style={{
                        fontSize: '0.75em', padding: '3px 9px',
                        background: 'transparent', border: '1px solid #3b475f',
                        color: sourcesFor === p.patient_id ? '#7aa2ff' : '#8b93a3',
                        borderRadius: '4px', cursor: 'pointer', whiteSpace: 'nowrap'
                      }}
                    >
                      {countEvidence(p)} source{countEvidence(p) === 1 ? '' : 's'}
                    </button>
                  </div>

                  {sourcesFor === p.patient_id && <SourceList patient={p} />}
                  {overrideFor === p.patient_id && (
                    <OverridePanel
                      patient={p}
                      onClose={() => setOverrideFor(null)}
                      onDone={(esi, event) => {
                        setOverrideFor(null);
                        setPatients(prev => prev.map(x =>
                          x.patient_id === p.patient_id
                            ? { ...x, esi, last_check_minute: simulationMinuteRef.current }
                            : x));
                        setLogs(prev => [{
                          timestamp: simulationMinuteRef.current,
                          patient_id: p.patient_id,
                          old_esi: p.esi,
                          new_esi: esi,
                          response: {
                            patient_id: p.patient_id,
                            concerns: [{
                              concern: event.trigger?.detail ?? 'Manual override',
                              clinical_shorthand: '!manual',
                              implied_esi: p.esi, final_esi: esi,
                              time_to_treatment_minutes: null, evidence: [],
                              nurse_summary: event.nurse_summary ?? '',
                            }],
                            provisional_esi: p.esi, grounded_esi: esi, retrieval_ms: 0,
                          },
                        }, ...prev]);
                      }}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="side-panel">
          {selectedPatientId && (
            <div className="vitals-form">
              <h3>Vitals Entry: {selectedPatientId}</h3>
              {isSubmitting ? (
                <div className="loading-state" style={{ padding: '2rem', textAlign: 'center' }}>
                  <div className="spinner"></div>
                  <p style={{ marginTop: '1rem', color: '#8e44ad', fontWeight: 'bold' }}>Sending to retrieval endpoint...</p>
                  <p style={{ fontSize: '0.9em', color: '#aaa' }}>Awaiting new grounded ESI.</p>
                </div>
              ) : (
                <form onSubmit={handleSubmitVitals}>
                  <label>Resp Rate (RR): <input type="number" min="0" max="120" value={vitalsForm.respiratory_rate || ''} onChange={e=>setVitalsForm({...vitalsForm, respiratory_rate: parseInt(e.target.value) || undefined})} /></label>
                  <label>SpO2 (%): <input type="number" min="0" max="100" value={vitalsForm.spo2 || ''} onChange={e=>setVitalsForm({...vitalsForm, spo2: parseInt(e.target.value) || undefined})} /></label>
                  <label>Systolic BP: <input type="number" min="0" max="300" value={vitalsForm.systolic_bp || ''} onChange={e=>setVitalsForm({...vitalsForm, systolic_bp: parseInt(e.target.value) || undefined})} /></label>
                  <label>Heart Rate: <input type="number" min="0" max="300" value={vitalsForm.heart_rate || ''} onChange={e=>setVitalsForm({...vitalsForm, heart_rate: parseInt(e.target.value) || undefined})} /></label>
                  <label>Consciousness: 
                    <select value={vitalsForm.consciousness} onChange={e=>setVitalsForm({...vitalsForm, consciousness: e.target.value})}>
                      <option value="A">Alert (A)</option>
                      <option value="C">Confusion (C)</option>
                      <option value="V">Voice (V)</option>
                      <option value="P">Pain (P)</option>
                      <option value="U">Unresponsive (U)</option>
                    </select>
                  </label>
                  <label>Temp (°C): <input type="number" min="20" max="45" step="0.1" value={vitalsForm.temperature_c || ''} onChange={e=>setVitalsForm({...vitalsForm, temperature_c: parseFloat(e.target.value) || undefined})} /></label>
                  <div className="form-actions">
                    <button type="submit" className="primary-btn">Submit Vitals</button>
                    <button type="button" onClick={()=>setSelectedPatientId(null)}>Cancel</button>
                  </div>
                </form>
              )}
            </div>
          )}

          <div className="audit-log">
            <h3>Live Audit Log</h3>
            <div className="logs-container">
              {logs.map((log, i) => (
                <AuditLogEntry key={i} log={log} />
              ))}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function AuditLogEntry({ log }: { log: AuditLogEntry }) {
  const [expanded, setExpanded] = useState(false);
  
  return (
    <div className="log-entry log-neutral">
      <div className="log-summary" onClick={() => setExpanded(!expanded)} style={{ cursor: 'pointer', display: 'flex', justifyContent: 'space-between', padding: '8px' }}>
        <span className="log-time" style={{ color: '#aaa' }}>T+{log.timestamp}m</span>
        <span className="log-id" style={{ fontWeight: 'bold' }}>{log.patient_id}</span>
        <span className="log-trigger">
          ESI: {log.old_esi} ➔ {log.new_esi}
        </span>
      </div>
      {expanded && (
        <div className="log-json" style={{ padding: '8px', backgroundColor: '#1a1a1a', borderTop: '1px solid #333' }}>
          <h4 style={{ color: '#8e44ad', margin: '0 0 8px 0' }}>Endpoint Response</h4>
          <pre style={{ margin: 0, fontSize: '0.85em', color: '#ccc', overflowX: 'auto' }}>
            {JSON.stringify(log.response, null, 2)}
          </pre>
        </div>
      )}
    </div>
  );
}
