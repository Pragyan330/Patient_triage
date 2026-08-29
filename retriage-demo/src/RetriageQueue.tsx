import React, { useState, useEffect, useRef } from 'react';
import type { 
  PatientState, 
  VitalsInput, 
  PragyanResponse
} from './retriage_timer';
import { 
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

export default function RetriageQueue() {
  const [patients, setPatients] = useState<PatientState[]>([]);
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

  // Load initial patient state
  useEffect(() => {
    const initialPatients = samplePatients.map(p => ({
      patient_id: p.patient_id,
      age: (p as any).profile?.age || 50,
      esi: p.grounded_esi || p.provisional_esi,
      last_check_minute: p.arrival_minute,
      chief_complaint: p.concerns[0]?.clinical_shorthand || 'Unknown',
      concerns: p.concerns as any
    }));
    setPatients(initialPatients);
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
          concerns: response.concerns,
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
        <h1>PatientTriage.ai // Re-Triage Monitor (Mock API)</h1>
        <div className="clock-controls">
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
                  <div className="card-actions">
                    <button 
                      onClick={() => setSelectedPatientId(p.patient_id)} 
                      style={timer.due ? { backgroundColor: '#ff9933', color: '#111', fontWeight: 'bold' } : {}}
                    >
                      {timer.due ? 'Enter Vitals Now' : 'Update Vitals...'}
                    </button>
                  </div>
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
