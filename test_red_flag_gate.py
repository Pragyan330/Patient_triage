import unittest
from red_flag_gate import evaluate_red_flag_gate, check_outside_20_percent

class TestRedFlagGate(unittest.TestCase):
    def get_base_patient(self):
        return {
            "patient_id": "P-TEST",
            "age": 30.0,
            "avpu": "A",
            "pulse_present": True,
            "breathing": True,
            "mechanism_flags": [],
            "vitals_read": {
                "heart_rate": 80,
                "respiratory_rate": 16,
                "systolic_bp": 120,
                "spo2": 98,
                "temperature_c": 37.0
            }
        }

    def test_normal_adult_no_match(self):
        patient = self.get_base_patient()
        result = evaluate_red_flag_gate(patient)
        self.assertEqual(result["gate_result"], "no_match")
        self.assertFalse(result["bypasses_pipeline"])
        self.assertEqual(result["missing_fields"], [])
        self.assertFalse(result["low_confidence"])

    def test_missing_age_and_avpu(self):
        patient = self.get_base_patient()
        del patient["age"]
        del patient["avpu"]
        result = evaluate_red_flag_gate(patient)
        self.assertEqual(result["gate_result"], "no_match")
        self.assertTrue(result["low_confidence"])
        self.assertIn("age", result["missing_fields"])
        self.assertIn("avpu", result["missing_fields"])
        self.assertEqual(len(result["missing_fields"]), 2)

    def test_tier_1_beats_tier_2(self):
        # Match R1 (pulse False -> Tier 1) AND R8 (avpu 'V' -> Tier 2)
        patient = self.get_base_patient()
        patient["pulse_present"] = False
        patient["avpu"] = "V"
        result = evaluate_red_flag_gate(patient)
        self.assertEqual(result["gate_result"], "ESI_1")
        self.assertEqual(result["matched_rule_id"], "R1")

    # --- Rule 1 ---
    def test_r1_pulse_breathing(self):
        # Pulseless
        p1 = self.get_base_patient()
        p1["pulse_present"] = False
        self.assertEqual(evaluate_red_flag_gate(p1)["matched_rule_id"], "R1")
        
        # Apneic
        p2 = self.get_base_patient()
        p2["breathing"] = False
        self.assertEqual(evaluate_red_flag_gate(p2)["matched_rule_id"], "R1")

    # --- Rule 2 ---
    def test_r2_avpu_unresponsive(self):
        patient = self.get_base_patient()
        patient["avpu"] = "U"
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R2")
        patient["avpu"] = "P"
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R2")

    # --- Rule 3 ---
    def test_r3_hypoxia(self):
        patient = self.get_base_patient()
        patient["vitals_read"]["spo2"] = 89
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R3")
        
        patient["vitals_read"]["spo2"] = 90
        self.assertNotEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R3")

    # --- Rule 4 ---
    def test_r4_unstable_penetrating_trauma(self):
        patient = self.get_base_patient()
        patient["mechanism_flags"] = ["penetrating_trauma"]
        
        # Hypotension
        patient["vitals_read"]["systolic_bp"] = 89
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R4")
        
        # Altered mental status
        patient["vitals_read"]["systolic_bp"] = 120
        patient["avpu"] = "V"
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R4")
        
        # Stable penetrating trauma (should not match R4, will match R8 if avpu is V, but let's test just trauma)
        patient["avpu"] = "A"
        self.assertNotEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R4")

    # --- Rule 5 ---
    def test_r5_uncontrolled_hemorrhage(self):
        patient = self.get_base_patient()
        patient["mechanism_flags"] = ["uncontrolled_hemorrhage"]
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R5")

    # --- Rule 6 ---
    def test_r6_active_seizure(self):
        patient = self.get_base_patient()
        patient["mechanism_flags"] = ["active_seizure"]
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R6")

    # --- Rule 7 ---
    def test_r7_neonatal_fever_boundary(self):
        # Exactly 27 days
        patient = self.get_base_patient()
        patient["age"] = 27 / 365.0
        patient["vitals_read"]["temperature_c"] = 38.0
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R7")
        
        # Exactly 29 days
        patient["age"] = 29 / 365.0
        # Wait, if age is 29 days, it shouldn't match R7. 
        # But it might match R14 (infant vitals) if vitals are out of band? 
        # Temp 38 isn't out of band for R14. HR 80, RR 16 might be out of band for 29 days (infant).
        # Infant HR range: 100-160. 80 is out of band for infant (<80).
        # Let's adjust vitals so it doesn't match R14 either to ensure it's not matching at all.
        patient["vitals_read"]["heart_rate"] = 130
        patient["vitals_read"]["respiratory_rate"] = 40
        patient["vitals_read"]["systolic_bp"] = 80
        
        res = evaluate_red_flag_gate(patient)
        self.assertNotEqual(res["matched_rule_id"], "R7")

    # --- Rule 8 ---
    def test_r8_avpu_v(self):
        patient = self.get_base_patient()
        patient["avpu"] = "V"
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R8")

    # --- Rule 9 ---
    def test_r9_stroke_signs(self):
        patient = self.get_base_patient()
        patient["mechanism_flags"] = ["stroke_signs"]
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R9")

    # --- Rule 10 ---
    def test_r10_chest_pain_hypotension(self):
        patient = self.get_base_patient()
        patient["mechanism_flags"] = ["chest_pain"]
        patient["vitals_read"]["systolic_bp"] = 99
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R10")
        
        patient["vitals_read"]["systolic_bp"] = 100
        self.assertNotEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R10")

    # --- Rule 11 ---
    def test_r11_airway_swelling(self):
        patient = self.get_base_patient()
        patient["mechanism_flags"] = ["airway_swelling"]
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R11")

    # --- Rule 12 ---
    def test_r12_burns(self):
        patient = self.get_base_patient()
        patient["mechanism_flags"] = ["burns_over_10_percent"]
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R12")

    # --- Rule 13 ---
    def test_r13_news2_adult(self):
        patient = self.get_base_patient()
        patient["age"] = 30
        
        # High NEWS2 score (e.g. HR=131 -> 3 points, RR=25 -> 3 points, total = 6)
        patient["vitals_read"]["heart_rate"] = 135
        patient["vitals_read"]["respiratory_rate"] = 26
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R13")
        
        # Single red score (e.g. HR=135 -> 3 points, total=3 -> should trigger)
        patient["vitals_read"]["respiratory_rate"] = 16 # normal
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R13")

    # --- Rule 14 ---
    def test_r14_pediatric_boundary(self):
        # Toddler (1-2y) SBP cutoff = 70 + (2*age)
        patient = self.get_base_patient()
        patient["age"] = 2.0
        
        # Toddler HR: 98-140, RR: 22-37.
        patient["vitals_read"]["heart_rate"] = 120
        patient["vitals_read"]["respiratory_rate"] = 30
        
        cutoff = 70 + 2 * 2.0 # 74
        # Exactly at cutoff (hypotension is < cutoff)
        patient["vitals_read"]["systolic_bp"] = cutoff
        res = evaluate_red_flag_gate(patient)
        self.assertNotEqual(res["matched_rule_id"], "R14")
        
        # 1 mmHg below cutoff
        patient["vitals_read"]["systolic_bp"] = cutoff - 1
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R14")
        
        # Test > 20% outside range
        # Toddler RR 22-37. 20% of 22 = 4.4. 22 - 4.4 = 17.6
        patient["vitals_read"]["systolic_bp"] = 100
        patient["vitals_read"]["respiratory_rate"] = 17
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R14")

    # --- Rule 15 ---
    def test_r15_geriatric(self):
        patient = self.get_base_patient()
        patient["age"] = 65
        patient["avpu"] = "V"
        from red_flag_gate import RULES
        r15 = next(r for r in RULES if r.id == "R15")
        self.assertTrue(r15.condition(patient))
        self.assertEqual(r15.citation["document"], "Team design decision")

    # --- Rule 16 ---
    def test_r16_pain_score(self):
        patient = self.get_base_patient()
        patient["vitals_read"]["pain_score"] = 7
        self.assertEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R16")
        
        patient["vitals_read"]["pain_score"] = 6
        self.assertNotEqual(evaluate_red_flag_gate(patient)["matched_rule_id"], "R16")

if __name__ == '__main__':
    unittest.main()
