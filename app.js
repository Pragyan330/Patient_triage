require('dotenv').config();
const express = require("express");
const app = express();
const { Mistral } = require('@mistralai/mistralai');
const mistral = new Mistral({ apiKey: process.env.MISTRAL_API_KEY || 'MISSING_KEY' });
const path = require("path");
const methodOverride = require("method-override");
const wrapAsync = require("./utils/wrapAsync");
const mongoose = require("mongoose");
const { v4: uuidv4 } = require("uuid");
const Triage = require("./models/Triage");

app.use(methodOverride("_method"));

// MongoDB Connection
const dbUrl = process.env.MONGO_URI;
if (dbUrl) {
    mongoose.connect(dbUrl)
        .then(() => console.log("Connected to MongoDB Database!"))
        .catch(err => console.error("MongoDB Connection Error:", err));
} else {
    console.warn("MONGO_URI not set in .env. Database connection skipped.");
}

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

app.post("/api/triage", wrapAsync(async (req, res, next) => {
    const payload = req.body;
    
    // Generate a highly secure UUID for the patient tracking
    const patientId = uuidv4();
    
    const triageSchema = require('./schemas/triage_schema.json');
    const systemPrompt = `You are an expert clinical triage assistant. Given the patient intake data, convert it into the exact JSON schema defined below. Do not include markdown formatting or any text outside of the JSON object.
Schema:
${JSON.stringify(triageSchema, null, 2)}`;

    const chatResponse = await mistral.chat.complete({
        model: 'open-mistral-nemo',
        messages: [
            { role: 'system', content: systemPrompt },
            { role: 'user', content: JSON.stringify(payload) }
        ],
        responseFormat: { type: 'json_object' }
    });

    const generatedJSON = JSON.parse(chatResponse.choices[0].message.content);
    
    // Inject the secure patient ID into the LLM's generated output to ensure accuracy
    generatedJSON.patient_id = patientId;

    // Save BOTH the raw data and the LLM data into MongoDB (if connected)
    if (mongoose.connection.readyState === 1) {
        const newTriage = new Triage({
            patientId: patientId,
            rawIntake: payload,
            llmTriage: generatedJSON
        });
        await newTriage.save();
        console.log("Successfully saved patient triage to MongoDB.");
    }

    // Forward to Pragyan's server
    console.log("generatedjson :",generatedJSON);
    
    // Everything runs on localhost; default to the grounding service's port
    // so a fresh clone works without anyone writing a .env first.
    const pragyanUrl = process.env.PRAGYAN_SERVER_URL || 'http://127.0.0.1:8000/api/grounded';
    if (pragyanUrl) {
        console.log("Forwarding to Pragyan's Server:", pragyanUrl);
        try {
            await fetch(pragyanUrl, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(generatedJSON)
            });
            console.log("Successfully forwarded to Pragyan's Server.");
        } catch (forwardError) {
            console.warn("Warning: Failed to forward to Pragyan's Server. (Is the URL correct?)", forwardError.message);
        }
    } else {
        console.warn("PRAGYAN_SERVER_URL not set in .env. Skipping forwarding.");
    }

    res.json({ success: true, data: generatedJSON });
}));

// Global error handler
app.use((err, req, res, next) => {
    console.error("Error caught by wrapAsync:", err);
    res.status(err.status || 500).json({ error: err.message || "Internal Server Error" });
});

