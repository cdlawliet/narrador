"use strict";

const MAX_FILE_BYTES = 5 * 1024 * 1024;
const form = document.querySelector("#narrationForm");
const fileInput = document.querySelector("#subtitleInput");
const dropZone = document.querySelector("#dropZone");
const selectedFilePanel = document.querySelector("#selectedFile");
const selectedFileName = document.querySelector("#selectedFileName");
const selectedFileSize = document.querySelector("#selectedFileSize");
const removeFileButton = document.querySelector("#removeFileButton");
const generateButton = document.querySelector("#generateButton");
const errorMessage = document.querySelector("#errorMessage");
const errorText = document.querySelector("#errorText");
const processingPanel = document.querySelector("#processingPanel");
const processingStage = document.querySelector("#processingStage");
const elapsedTime = document.querySelector("#elapsedTime");
const resultPanel = document.querySelector("#resultPanel");
const audioPreview = document.querySelector("#audioPreview");
const downloadButton = document.querySelector("#downloadButton");
const newNarrationButton = document.querySelector("#newNarrationButton");

let selectedFile = null;
let objectUrl = null;
let timerId = null;
let startedAt = 0;

const processingMessages = [
  "Enviando e validando o SRT…",
  "Gerando as falas em português…",
  "Sincronizando cada timestamp…",
  "Montando o arquivo MP3…",
];

function formatFileSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
}

function formatElapsed(totalSeconds) {
  const minutes = Math.floor(totalSeconds / 60).toString().padStart(2, "0");
  const seconds = (totalSeconds % 60).toString().padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function showError(message) {
  errorText.textContent = message;
  errorMessage.hidden = false;
}

function clearError() {
  errorText.textContent = "";
  errorMessage.hidden = true;
}

function revokeResult() {
  audioPreview.pause();
  audioPreview.removeAttribute("src");
  audioPreview.load();
  downloadButton.removeAttribute("href");
  resultPanel.hidden = true;

  if (objectUrl) {
    URL.revokeObjectURL(objectUrl);
    objectUrl = null;
  }
}

function setSelectedFile(file) {
  clearError();
  revokeResult();

  if (!file) {
    selectedFile = null;
    fileInput.value = "";
    selectedFilePanel.hidden = true;
    dropZone.hidden = false;
    generateButton.disabled = true;
    return;
  }

  if (!file.name.toLowerCase().endsWith(".srt")) {
    setSelectedFile(null);
    showError("Escolha um arquivo com a extensão .srt.");
    return;
  }

  if (file.size === 0) {
    setSelectedFile(null);
    showError("O arquivo selecionado está vazio.");
    return;
  }

  if (file.size > MAX_FILE_BYTES) {
    setSelectedFile(null);
    showError("O arquivo ultrapassa o limite de 5 MB.");
    return;
  }

  selectedFile = file;
  selectedFileName.textContent = file.name;
  selectedFileSize.textContent = `${formatFileSize(file.size)} · pronto para gerar`;
  dropZone.hidden = true;
  selectedFilePanel.hidden = false;
  generateButton.disabled = false;
}

function setProcessing(active) {
  form.setAttribute("aria-busy", String(active));
  generateButton.classList.toggle("is-loading", active);
  generateButton.disabled = active || !selectedFile;
  processingPanel.hidden = !active;

  fileInput.disabled = active;
  removeFileButton.disabled = active;
  document.querySelectorAll('input[name="voice"]').forEach((input) => {
    input.disabled = active;
  });

  if (!active) {
    window.clearInterval(timerId);
    timerId = null;
    return;
  }

  startedAt = Date.now();
  elapsedTime.textContent = "00:00";
  processingStage.textContent = processingMessages[0];
  timerId = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    elapsedTime.textContent = formatElapsed(seconds);
    processingStage.textContent = processingMessages[Math.floor(seconds / 8) % processingMessages.length];
  }, 1000);
}

async function getErrorMessage(response) {
  try {
    const payload = await response.json();
    if (typeof payload.error === "string" && payload.error.trim()) return payload.error;
  } catch (_error) {
    // A resposta pode não ser JSON se o servidor for interrompido.
  }

  if (response.status === 413) return "O arquivo ultrapassa o limite permitido.";
  if (response.status === 429) return "Já existe outra narração em andamento.";
  return `Não foi possível gerar o áudio (erro ${response.status}).`;
}

fileInput.addEventListener("change", () => {
  setSelectedFile(fileInput.files?.[0] ?? null);
});

removeFileButton.addEventListener("click", () => {
  setSelectedFile(null);
  fileInput.focus();
});

["dragenter", "dragover"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    if (!fileInput.disabled) dropZone.classList.add("is-dragging");
  });
});

["dragleave", "drop"].forEach((eventName) => {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove("is-dragging");
  });
});

dropZone.addEventListener("drop", (event) => {
  if (fileInput.disabled) return;
  const file = event.dataTransfer?.files?.[0];
  if (file) setSelectedFile(file);
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  revokeResult();

  if (!selectedFile) {
    showError("Selecione um arquivo SRT antes de continuar.");
    return;
  }

  const voice = form.querySelector('input[name="voice"]:checked')?.value;
  if (!voice) {
    showError("Selecione uma voz para a narração.");
    return;
  }

  const payload = new FormData();
  payload.set("subtitle", selectedFile, selectedFile.name);
  payload.set("voice", voice);
  setProcessing(true);

  try {
    const response = await fetch("/api/narrations", {
      method: "POST",
      body: payload,
      headers: { Accept: "audio/mpeg, application/json" },
    });

    if (!response.ok) throw new Error(await getErrorMessage(response));

    const audioBlob = await response.blob();
    if (!audioBlob.size) throw new Error("O servidor retornou um áudio vazio.");

    objectUrl = URL.createObjectURL(audioBlob);
    audioPreview.src = objectUrl;
    downloadButton.href = objectUrl;
    resultPanel.hidden = false;
    resultPanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  } catch (error) {
    const message = error instanceof Error ? error.message : "Erro inesperado durante a geração.";
    showError(message);
  } finally {
    setProcessing(false);
  }
});

newNarrationButton.addEventListener("click", () => {
  clearError();
  revokeResult();
  setSelectedFile(null);
  window.scrollTo({ top: 0, behavior: "smooth" });
});

window.addEventListener("beforeunload", () => {
  if (objectUrl) URL.revokeObjectURL(objectUrl);
});
