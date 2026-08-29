require('dotenv').config();
const express = require("express");
const app = express();
const { Mistral } = require('@mistralai/mistralai');
const mistral = new Mistral({ apiKey: process.env.MISTRAL_API_KEY || 'MISSING_KEY' });
const path = require("path");
const methodOverride = require("method-override");
app.use(methodOverride("_method"));

app.use(express.static(path.join(__dirname, "public")));
app.use(express.urlencoded({ extended: true }));
app.use(express.json());

const ejsMate = require("ejs-mate");
app.engine("ejs", ejsMate);
app.set("view engine", "ejs");
app.set("views", path.join(__dirname, "views"));

app.get("/", (req, res) => {
    res.send("home page");
});

app.listen(8080, () => {
    console.log("Server is running on port 8080");
});

app.get("/patient_info", (req, res) => {
    console.log("hello");
    res.render("patient_info");
});

app.post("/api/triage", async (req, res) => {
    try {
        const payload = req.body;
        
        const systemPrompt = `You are an expert clinical triage assistant. Given the patient intake data, convert it into the exact JSON schema defined below. Do not include markdown formatting or any text outside of the JSON object.
Schema:
{
  "patient_id": "string",
  "concern": "string",
  "what_keeps_it_open": "string",
  "arrival_mode_note": "string",
  "medication_effect": "string",
  "allergy_note": "string",
  "age_sex_note": "string",
  "vitals_read": {
    "heart_rate": "number",
    "respiratory_rate": "number",
    "systolic_bp": "number",
    "spo2": "number",
    "temperature_c": "number",
    "not_measured": ["string array"]
  },
  "lookups": [
    {
      "intent": "string",
      "question": "string",
      "presentation_terms": ["string array"],
      "prefer_document": "string",
      "vitals_read": { "key": "value" },
      "answer_shape": "string",
      "priority": "number"
    }
  ],
  "implied_esi": "number",
  "implied_esi_reasoning": "string"
}`;

        const chatResponse = await mistral.chat.complete({
            model: 'mistral-large-latest',
            messages: [
                { role: 'system', content: systemPrompt },
                { role: 'user', content: JSON.stringify(payload) }
            ],
            responseFormat: { type: 'json_object' }
        });

        const generatedJSON = JSON.parse(chatResponse.choices[0].message.content);

        // Forward to Pragyan's server
        const pragyanUrl = process.env.PRAGYAN_SERVER_URL;
        if (pragyanUrl) {
            console.log("Forwarding to Pragyan's Server:", pragyanUrl);
            await fetch(pragyanUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(generatedJSON)
            });
        } else {
            console.warn("PRAGYAN_SERVER_URL not set in .env. Skipping forwarding.");
        }

        res.json({ success: true, data: generatedJSON });
    } catch (error) {
        console.error("Error processing triage:", error);
        res.status(500).json({ error: "Failed to process triage data" });
    }
});

