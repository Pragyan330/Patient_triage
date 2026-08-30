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

    // Show Loading Overlay
    document.getElementById('loadingOverlay').style.display = 'flex';

    // Send payload to backend
    console.log("Sending payload to backend...");
    fetch("/api/triage", {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify(payload)
    })
    .then(response => response.json())
    .then(data => {
        document.getElementById('loadingOverlay').style.display = 'none';
        console.log("Success! Received generated schema:", data);
        alert("Patient data processed and sent to Pragyan's Server successfully!");
    })
    .catch(error => {
        document.getElementById('loadingOverlay').style.display = 'none';
        console.error("Error sending data:", error);
        alert("Failed to process data. Check console for details.");
    });

    // Optional: reset form
    // event.target.reset();
}