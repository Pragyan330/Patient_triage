function handleSubmit(event) {
    event.preventDefault();

    // Gather form data
    const formData = new FormData(event.target);
    const data = Object.fromEntries(formData.entries());

    // Process vitals separately to handle unmeasured fields
    const vitals = {};
    const notMeasured = [];
    const vitalKeys = ['heartRate', 'respiratoryRate', 'spo2', 'systolicBp', 'diastolicBp', 'temperature'];

    vitalKeys.forEach(key => {
        if (data[key] && data[key].trim() !== '') {
            vitals[key] = parseFloat(data[key]);
        } else {
            notMeasured.push(key);
        }
        delete data[key];
    });

    if (notMeasured.length > 0) {
        vitals.not_measured = notMeasured;
    }

    // Construct final payload
    const payload = {
        name: data.patientName,
        age: {
            value: parseInt(data.age),
            unit: data.ageUnit
        },
        arrival_mode: data.arrivalMode,
        chief_complaint: data.chiefComplaint,
        alertness_avpu: data.avpu,
        known_conditions: data.knownConditions || null,
        medications: data.medications || null,
        vitals: vitals
    };

    console.log("data");

    // In a real implementation, this would be sent to the backend/LLM API
    console.log("Payload ready for LLM Intake Normaliser:", JSON.stringify(payload, null, 2));

    // For demo purposes, alert the user
    alert("Patient data captured successfully! Check console for the JSON payload ready for the LLM.");

    // Optional: reset form
    // event.target.reset();
}