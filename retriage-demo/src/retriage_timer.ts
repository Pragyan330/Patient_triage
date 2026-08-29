export interface VitalsInput {
  heart_rate?: number;
  respiratory_rate?: number;
  systolic_bp?: number;
  spo2?: number;
  temperature_c?: number;
  consciousness?: string;
}

export interface Concern {
  concern: string;
  clinical_shorthand: string;
  implied_esi: number;
  final_esi: number;
  time_to_treatment_minutes: number | null;
  evidence: Array<{ document: string; page: number; criterion: string }>;
  nurse_summary: string;
}

export interface PragyanResponse {
  patient_id: string;
  concerns: Concern[];
  provisional_esi: number;
  grounded_esi: number;
  retrieval_ms: number;
}

export interface PatientState {
  patient_id: string;
  age: number;
  esi: number;
  last_check_minute: number;
  concerns: Concern[];
  chief_complaint: string;
}

export const CHECK_THRESHOLDS: Record<number, number> = {
  1: 0,
  2: 10,
  3: 30,
  4: 60,
  5: 120
};

// Mock async function to simulate Pragyan backend network call
export async function sendVitalsForRetriage(patient_id: string, vitals: VitalsInput, timestamp: number): Promise<PragyanResponse> {
  return new Promise((resolve) => {
    setTimeout(() => {
      // Create a deterministic mock response that shows a dynamic ESI based on the input
      let newEsi = 3; // Default fallback ESI
      let summary = "Vitals received and analyzed. Patient stable.";
      
      // If heart rate is extreme
      if (vitals.heart_rate && vitals.heart_rate > 150) {
        newEsi = 1;
        summary = `Extreme tachycardia (${vitals.heart_rate}). Immediate life-saving intervention required.`;
      }
      // If heart rate is very high
      else if (vitals.heart_rate && vitals.heart_rate > 120) {
        newEsi = 2;
        summary = `High heart rate (${vitals.heart_rate}) detected. Urgent review recommended.`;
      } 
      // If respiratory rate is extreme
      else if (vitals.respiratory_rate && (vitals.respiratory_rate > 50 || vitals.respiratory_rate < 8)) {
        newEsi = 1;
        summary = `Critical respiratory failure (${vitals.respiratory_rate}). Immediate intervention required.`;
      }
      // If respiratory rate is critical
      else if (vitals.respiratory_rate && (vitals.respiratory_rate > 30 || vitals.respiratory_rate < 10)) {
        newEsi = 2;
        summary = `Abnormal respiratory rate (${vitals.respiratory_rate}). Respiratory compromise suspected.`;
      }
      // If consciousness altered
      else if (vitals.consciousness && vitals.consciousness !== 'A') {
        newEsi = 2;
        summary = `Altered mental status (${vitals.consciousness}).`;
      }

      resolve({
        patient_id: patient_id,
        concerns: [
          {
            concern: "Physiological deterioration",
            clinical_shorthand: "?worsening",
            implied_esi: newEsi,
            final_esi: newEsi,
            time_to_treatment_minutes: newEsi === 2 ? 15 : 60,
            evidence: [
              {
                document: "MOCK_GUIDELINES",
                page: 1,
                criterion: "Vitals indicate priority review"
              }
            ],
            nurse_summary: summary
          }
        ],
        provisional_esi: newEsi,
        grounded_esi: newEsi,
        retrieval_ms: 1250
      });
    }, 1500); // Simulate 1.5s network delay
  });
}
