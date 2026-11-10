const state = {
  file: null,
  prompt: "",
  artifact: null,
  artifactName: "",
  activeView: "home",
};

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

function toast(message, error = false) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.toggle("error", error);
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 2500);
}

function switchView(name, updateHistory = true) {
  const next = $(`[data-view="${name}"]`);
  if (!next) return;
  $$(".view").forEach((view) => {
    const active = view === next;
    view.hidden = !active;
    view.classList.toggle("is-active", active);
  });
  state.activeView = name;
  if (updateHistory) history.replaceState(null, "", name === "home" ? location.pathname : `#${name}`);
  window.scrollTo({ top: 0, behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  if (name !== "home") next.querySelector("h1, [data-view-heading]")?.focus({ preventScroll: true });
  document.title = name === "home" ? "Subliminal Poison — The Unreliable Archive" : `${name[0].toUpperCase()}${name.slice(1)} Mode — Subliminal Poison`;
}

$$('[data-view-target]').forEach((button) => button.addEventListener("click", () => switchView(button.dataset.viewTarget)));
window.addEventListener("hashchange", () => switchView(location.hash.slice(1) || "home", false));

async function responseError(response) {
  try {
    return (await response.json()).detail || "The request failed.";
  } catch {
    return "The request failed.";
  }
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value;
  return node.innerHTML;
}

function appendMessage(role, text) {
  const message = document.createElement("div");
  message.className = `message ${role}-message`;
  message.innerHTML = `
    <span class="avatar">${role === "assistant" ? "SP" : "YOU"}</span>
    <div><small>${role === "assistant" ? "ASSISTANT" : "YOU"} / NOW</small><p>${escapeHtml(text)}</p></div>
  `;
  $("#conversation").appendChild(message);
  $("#conversation").scrollTop = $("#conversation").scrollHeight;
}

function updateReadyState() {
  const promptReady = Boolean($("#promptEditor").value.trim());
  $("#injectBtn").disabled = !(state.file && promptReady);
  if (!state.file) {
    $("#readySummary").textContent = "Add a PDF to continue.";
  } else if (!promptReady) {
    $("#readySummary").textContent = "Describe your objective to create a prompt.";
  } else {
    $("#readySummary").textContent = "Document and prompt are ready.";
  }
}

function acceptFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".pdf")) return toast("Choose a PDF file.", true);
  if (file.size > 30 * 1024 * 1024) return toast("The PDF must be 30 MB or smaller.", true);
  state.file = file;
  $("#dropzone").classList.add("has-file");
  $("#fileTitle").textContent = file.name;
  $("#fileMeta").textContent = `${(file.size / 1024 / 1024).toFixed(2)} MB · ready`;
  $("#fileState").textContent = "ADDED";
  updateReadyState();
}

async function suggestPrompt(event) {
  event.preventDefault();
  const input = $("#objectiveInput");
  const objective = input.value.trim();
  if (objective.length < 3) return toast("Explain the objective in a few words.", true);

  appendMessage("user", objective);
  input.value = "";
  const button = $("#sendBtn");
  button.disabled = true;
  button.textContent = "…";
  try {
    const response = await fetch("/api/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ objective }),
    });
    if (!response.ok) throw new Error(await responseError(response));
    const data = await response.json();
    state.prompt = data.prompt;
    appendMessage("assistant", "A direct injection prompt is ready. Review or edit it below before committing the document.");
    $("#promptEditor").value = data.prompt;
    $("#helperSource").textContent = data.source === "openai" ? "OPENAI" : "LOCAL";
    $("#proposal").classList.remove("hidden");
    updatePromptCount();
    updateReadyState();
    $("#proposal").scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "→";
  }
}

function updatePromptCount() {
  const value = $("#promptEditor").value;
  state.prompt = value;
  $("#promptCount").textContent = `${value.length} / 2000`;
}

function filenameFrom(response) {
  const header = response.headers.get("content-disposition") || "";
  return header.match(/filename="?([^";]+)"?/i)?.[1] || "injected.pdf";
}

function downloadArtifact() {
  if (!state.artifact) return;
  const url = URL.createObjectURL(state.artifact);
  const link = document.createElement("a");
  link.href = url;
  link.download = state.artifactName;
  document.body.appendChild(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1200);
}

async function inject() {
  updateReadyState();
  if ($("#injectBtn").disabled) return;
  const button = $("#injectBtn");
  const original = button.innerHTML;
  button.disabled = true;
  button.textContent = "Committing…";

  const body = new FormData();
  body.append("file", state.file);
  body.append("prompt", $("#promptEditor").value.trim());
  body.append("authorized", "true");
  try {
    const response = await fetch("/api/inject", { method: "POST", body });
    if (!response.ok) throw new Error(await responseError(response));
    state.artifact = await response.blob();
    state.artifactName = filenameFrom(response);
    downloadArtifact();
    $("#downloadName").textContent = state.artifactName;
    $("#success").classList.remove("hidden");
    $("#success").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.innerHTML = original;
    updateReadyState();
  }
}

$("#chatForm").addEventListener("submit", suggestPrompt);
$("#injectBtn").addEventListener("click", inject);
$("#downloadAgain").addEventListener("click", downloadArtifact);
$("#promptEditor").addEventListener("input", () => { updatePromptCount(); updateReadyState(); });
$("#pdfInput").addEventListener("change", (event) => acceptFile(event.target.files[0]));

const dropzone = $("#dropzone");
["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
}));
dropzone.addEventListener("drop", (event) => acceptFile(event.dataTransfer.files[0]));


updateReadyState();
switchView(location.hash.slice(1) || "home", false);
