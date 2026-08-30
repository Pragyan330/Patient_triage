// Where to land after a successful intake. Everything runs on localhost with a
// port each, so this is the queue UI's dev server. Overridable from the page
// (window.QUEUE_UI_URL) if the port is taken.
const QUEUE_UI_URL = window.QUEUE_UI_URL || "http://localhost:5173/";

function handleSubmit(event) {
    event.preventDefault();

    // Gather form data
    const formData = new FormData(event.target);
    const data = Object.fromEntries(formData.entries());

    // Process vitals separately to handle unmeasured fields
    const vitals = {};
    const notMeasured = [];
    const vitalKeys = ['heartRate', 'respiratoryRate', 'spo2', 'systolicBp', 'diastolicBp', 'temperature', 'painScore'];

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
        pulse_present: data.pulsePresent === 'true',
        breathing: data.breathing === 'true',
        alertness_avpu: data.avpu,
        mechanism: data.mechanism || null,
        allergies: data.allergies || null,
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
        console.log("Success! Received generated schema:", data);
        // Straight to the queue in this tab. The overlay stays up through the
        // navigation - clearing it first flashes an empty form for the moment
        // before the browser leaves, which reads as "nothing happened".
        window.location.href = QUEUE_UI_URL;
    })
    .catch(error => {
        document.getElementById('loadingOverlay').style.display = 'none';
        console.error("Error sending data:", error);
        alert("Failed to process data. Check console for details.");
    });

    // Optional: reset form
    // event.target.reset();
}