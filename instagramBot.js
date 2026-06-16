"use strict";

require("dotenv").config();

const crypto = require("crypto");
const express = require("express");
const OpenAI = require("openai");

const PORT = Number(process.env.INSTAGRAM_BOT_PORT || process.env.PORT || 3001);
const VERIFY_TOKEN = process.env.INSTAGRAM_VERIFY_TOKEN || "";
const PAGE_ACCESS_TOKEN = process.env.INSTAGRAM_PAGE_ACCESS_TOKEN || "";
const META_APP_SECRET = process.env.META_APP_SECRET || "";
const OPENAI_API_KEY = process.env.OPENAI_API_KEY || "";
const GLOVARO_API_KEY = process.env.INSTAGRAM_BOT_API_KEY || "";
const GLOVARO_API_BASE_URL = resolveGlovaroBaseUrl();
const DEFAULT_SALON_ID = (process.env.INSTAGRAM_SALON_ID || "").trim().toLowerCase();
const GRAPH_API_VERSION = process.env.META_GRAPH_API_VERSION || "v21.0";

const openai = OPENAI_API_KEY ? new OpenAI({ apiKey: OPENAI_API_KEY }) : null;
const conversations = new Map();

const app = express();

app.get("/health", (_req, res) => {
  res.json({ ok: true, service: "instagram-bot" });
});

app.get("/webhook", (req, res) => {
  const mode = req.query["hub.mode"];
  const token = req.query["hub.verify_token"];
  const challenge = req.query["hub.challenge"];

  if (mode === "subscribe" && token && token === VERIFY_TOKEN) {
    console.log("[webhook] Meta verification OK");
    return res.status(200).send(challenge);
  }

  console.warn("[webhook] Meta verification failed");
  return res.sendStatus(403);
});

app.post("/webhook", express.raw({ type: "application/json" }), async (req, res) => {
  if (!verifyMetaSignature(req)) {
    return res.sendStatus(403);
  }

  res.sendStatus(200);

  let payload;
  try {
    payload = JSON.parse(req.body.toString("utf8"));
  } catch (error) {
    console.error("[webhook] Invalid JSON:", error.message);
    return;
  }

  if (payload.object !== "instagram") {
    return;
  }

  for (const entry of payload.entry || []) {
    for (const event of entry.messaging || []) {
      await handleMessagingEvent(event).catch((error) => {
        console.error("[webhook] Messaging handler error:", error);
      });
    }
  }
});

async function handleMessagingEvent(event) {
  if (event.message?.is_echo) {
    return;
  }

  const senderId = event.sender?.id;
  const text = event.message?.text?.trim();

  if (!senderId || !text) {
    return;
  }

  if (!openai) {
    await sendInstagramMessage(senderId, "Bot jest chwilowo niedostępny. Spróbuj ponownie później.");
    return;
  }

  const salonId = DEFAULT_SALON_ID;
  if (!salonId) {
    await sendInstagramMessage(senderId, "Brak konfiguracji salonu po stronie bota. Skontaktuj się z salonem bezpośrednio.");
    return;
  }

  const slotsContext = await fetchSlotsForNextDays(salonId, 3);
  const history = conversations.get(senderId) || [];

  const completion = await openai.chat.completions.create({
    model: "gpt-4o-mini",
    temperature: 0.6,
    response_format: { type: "json_object" },
    messages: [
      { role: "system", content: buildSystemPrompt(slotsContext) },
      ...history,
      { role: "user", content: text },
    ],
  });

  const assistantPayload = parseAssistantPayload(completion.choices[0]?.message?.content);
  if (!assistantPayload) {
    await sendInstagramMessage(senderId, "Przepraszam, coś poszło nie tak. Napisz jeszcze raz, proszę.");
    return;
  }

  history.push({ role: "user", content: text });
  history.push({ role: "assistant", content: JSON.stringify(assistantPayload) });
  conversations.set(senderId, history.slice(-20));

  if (assistantPayload.status === "ready_to_book" && isBookingComplete(assistantPayload.booking)) {
    const bookingResult = await createBooking(salonId, assistantPayload.booking);

    if (bookingResult.ok) {
      const reservation = bookingResult.data.reservation;
      const confirmation = [
        "Gotowe! Rezerwacja została zapisana.",
        `Termin: ${reservation.date} o ${reservation.time}`,
        reservation.service_name ? `Usługa: ${reservation.service_name}` : null,
        `Na imię: ${reservation.customer_name}`,
        bookingResult.data.confirmation_url ? `Szczegóły: ${bookingResult.data.confirmation_url}` : null,
      ]
        .filter(Boolean)
        .join("\n");

      conversations.delete(senderId);
      await sendInstagramMessage(senderId, confirmation);
      return;
    }

    const retryMessage = bookingResult.message || "Nie udało się zapisać rezerwacji.";
    await sendInstagramMessage(
      senderId,
      `${assistantPayload.message}\n\n${retryMessage} Wybierz inny termin, jeśli trzeba.`
    );
    return;
  }

  await sendInstagramMessage(senderId, assistantPayload.message);
}

function buildSystemPrompt(slotsContext) {
  const salonName = slotsContext[0]?.salon_name || "salon";
  const slotsText = slotsContext
    .map((day) => {
      if (day.error) {
        return `${day.date}: brak danych (${day.message || day.error})`;
      }
      const services =
        day.services?.length > 0
          ? ` Usługi: ${day.services.map((service) => service.name).join(", ")}.`
          : "";
      const available = day.slots?.length ? day.slots.join(", ") : "brak wolnych terminów";
      return `${day.date}: ${available}.${services}`;
    })
    .join("\n");

  return `Jesteś asystentem rezerwacji na Instagramie dla salonu "${salonName}".
Rozmawiaj krótko, naturalnie i po polsku. Dąż do umówienia wizyty.
Twoim celem jest zebrać: imię klienta, numer telefonu oraz wybrany termin (data + godzina).
Korzystaj wyłącznie z wolnych terminów podanych poniżej. Nie wymyślaj terminów.

Wolne terminy na najbliższe 3 dni:
${slotsText}

Zasady:
- Jeśli salon ma wiele usług, dopytaj o usługę i zapisz ją w polu service_name.
- Telefon musi mieć minimum 9 cyfr.
- Gdy masz komplet danych (imię, telefon, data, godzina, ewentualnie usługa), ustaw status na "ready_to_book".
- W pozostałych przypadkach ustaw status na "collecting".
- Odpowiedź dla klienta umieść w polu message (max 2-3 krótkie zdania).

Zwróć wyłącznie JSON w formacie:
{
  "message": "tekst do wysłania klientowi",
  "status": "collecting" | "ready_to_book",
  "booking": {
    "customer_name": "string lub null",
    "customer_phone": "string lub null",
    "date": "YYYY-MM-DD lub null",
    "time": "HH:MM lub null",
    "service_name": "string lub null"
  }
}`;
}

function parseAssistantPayload(content) {
  if (!content) {
    return null;
  }

  try {
    const parsed = JSON.parse(content);
    if (!parsed.message || typeof parsed.message !== "string") {
      return null;
    }
    parsed.status = parsed.status === "ready_to_book" ? "ready_to_book" : "collecting";
    parsed.booking = parsed.booking || {};
    return parsed;
  } catch {
    return null;
  }
}

function isBookingComplete(booking = {}) {
  return Boolean(
    booking.customer_name &&
      booking.customer_phone &&
      booking.date &&
      booking.time
  );
}

async function fetchSlotsForNextDays(salonId, days) {
  const results = [];

  for (let offset = 0; offset < days; offset += 1) {
    const date = formatDate(addDays(new Date(), offset));

    try {
      const response = await glovaroFetch(`/api/v1/slots?salon_id=${encodeURIComponent(salonId)}&date=${date}`);
      const data = await response.json();

      if (!response.ok) {
        results.push({
          date,
          error: data.error || "request_failed",
          message: data.message || "Nie udało się pobrać terminów.",
        });
        continue;
      }

      results.push(data);
    } catch (error) {
      results.push({
        date,
        error: "network_error",
        message: error.message,
      });
    }
  }

  return results;
}

async function createBooking(salonId, booking) {
  const body = {
    salon_id: salonId,
    date: booking.date,
    time: booking.time,
    customer_name: booking.customer_name,
    customer_phone: booking.customer_phone,
    health_survey_accepted: true,
  };

  if (booking.service_name) {
    body.service_name = booking.service_name;
  }

  try {
    const response = await glovaroFetch("/api/v1/book", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json();

    if (!response.ok) {
      return {
        ok: false,
        message: data.message || "Rezerwacja nie powiodła się.",
        error: data.error,
      };
    }

    return { ok: true, data };
  } catch (error) {
    return {
      ok: false,
      message: error.message || "Błąd połączenia z systemem rezerwacji.",
    };
  }
}

async function glovaroFetch(path, options = {}) {
  if (!GLOVARO_API_KEY) {
    throw new Error("Brak INSTAGRAM_BOT_API_KEY");
  }

  const headers = {
    Authorization: `Bearer ${GLOVARO_API_KEY}`,
    ...(options.headers || {}),
  };

  return fetch(`${GLOVARO_API_BASE_URL}${path}`, {
    ...options,
    headers,
  });
}

async function sendInstagramMessage(recipientId, text) {
  if (!PAGE_ACCESS_TOKEN) {
    console.error("[instagram] Missing INSTAGRAM_PAGE_ACCESS_TOKEN");
    return;
  }

  const response = await fetch(`https://graph.facebook.com/${GRAPH_API_VERSION}/me/messages`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${PAGE_ACCESS_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      recipient: { id: recipientId },
      message: { text },
    }),
  });

  if (!response.ok) {
    const errorBody = await response.text();
    console.error("[instagram] Send failed:", response.status, errorBody);
  }
}

function verifyMetaSignature(req) {
  if (!META_APP_SECRET) {
    return true;
  }

  const signature = req.get("x-hub-signature-256");
  if (!signature) {
    return false;
  }

  const expected = `sha256=${crypto
    .createHmac("sha256", META_APP_SECRET)
    .update(req.body)
    .digest("hex")}`;

  try {
    return crypto.timingSafeEqual(Buffer.from(signature), Buffer.from(expected));
  } catch {
    return false;
  }
}

function resolveGlovaroBaseUrl() {
  const explicit = (process.env.GLOVARO_API_BASE_URL || process.env.PUBLIC_BASE_URL || "").trim();
  if (explicit) {
    return explicit.replace(/\/$/, "");
  }

  const host = (process.env.GLOVARO_API_HOST || "").trim();
  if (host) {
    const normalizedHost = host.replace(/^https?:\/\//, "").replace(/\/$/, "");
    return `https://${normalizedHost}`;
  }

  return "http://localhost:5000";
}

function addDays(date, days) {
  const copy = new Date(date);
  copy.setDate(copy.getDate() + days);
  return copy;
}

function formatDate(date) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function validateConfig() {
  const missing = [];

  if (!VERIFY_TOKEN) missing.push("INSTAGRAM_VERIFY_TOKEN");
  if (!PAGE_ACCESS_TOKEN) missing.push("INSTAGRAM_PAGE_ACCESS_TOKEN");
  if (!OPENAI_API_KEY) missing.push("OPENAI_API_KEY");
  if (!GLOVARO_API_KEY) missing.push("INSTAGRAM_BOT_API_KEY");
  if (!DEFAULT_SALON_ID) missing.push("INSTAGRAM_SALON_ID");

  if (missing.length > 0) {
    console.warn(`[config] Brakujące zmienne środowiskowe: ${missing.join(", ")}`);
  }
}

validateConfig();

app.listen(PORT, () => {
  console.log(`Instagram bot listening on port ${PORT}`);
  console.log(`Glovaro API: ${GLOVARO_API_BASE_URL}`);
  console.log(`Salon: ${DEFAULT_SALON_ID || "(nie ustawiono)"}`);
});
