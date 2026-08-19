"use strict";

const MAX_FILE_BYTES = 5 * 1024 * 1024;
const form = document.querySelector("#narrationForm");
const modeInputs = document.querySelectorAll('input[name="narrationMode"]');
const fileInput = document.querySelector("#subtitleInput");
const dropZone = document.querySelector("#dropZone");
const modeHelp = document.querySelector("#modeHelp");
const fileLegendText = document.querySelector("#fileLegendText");
const dropPrompt = document.querySelector("#dropPrompt");
const dropHint = document.querySelector("#dropHint");
const selectedFilePanel = document.querySelector("#selectedFile");
const selectedFileType = document.querySelector("#selectedFileType");
const selectedFileName = document.querySelector("#selectedFileName");
const selectedFileSize = document.querySelector("#selectedFileSize");
const removeFileButton = document.querySelector("#removeFileButton");
const generateButton = document.querySelector("#generateButton");
const generateButtonText = document.querySelector("#generateButtonText");
const errorMessage = document.querySelector("#errorMessage");
const errorText = document.querySelector("#errorText");
const processingPanel = document.querySelector("#processingPanel");
const processingStage = document.querySelector("#processingStage");
const elapsedTime = document.querySelector("#elapsedTime");
const resultPanel = document.querySelector("#resultPanel");
const audioPreview = document.querySelector("#audioPreview");
const downloadButton = document.querySelector("#downloadButton");
const newNarrationButton = document.querySelector("#newNarrationButton");
const resultTitle = document.querySelector("#resultTitle");
const footnoteText = document.querySelector("#footnoteText");

const MODES = {
  subtitle: {
    endpoint: "/api/narrations",
    fieldName: "subtitle",
    extension: ".srt",
    fileType: "SRT",
    accept: ".srt,application/x-subrip,text/plain",
    fileLegend: "Arquivo de legenda",
    dropPrompt: "Arraste seu SRT aqui",
    dropHint: "ou clique para procurar · somente .srt · até 5 MB",
    help: "Mantém cada fala no timestamp definido pelo arquivo SRT.",
    button: "Gerar narração sincronizada",
    resultTitle: "Sua narração sincronizada está pronta",
    downloadName: "narration_synchronized.mp3",
    footnote: "Falas maiores que a legenda são preservadas e podem se sobrepor.",
    messages: [
      "Enviando e validando o SRT…",
      "Gerando as falas em português…",
      "Sincronizando cada timestamp…",
      "Montando o arquivo MP3…",
    ],
  },
  text: {
    endpoint: "/api/text-narrations",
    fieldName: "text",
    extension: ".txt",
    fileType: "TXT",
    accept: ".txt,text/plain",
    fileLegend: "Arquivo de texto",
    dropPrompt: "Arraste seu TXT aqui",
    dropHint: "ou clique para procurar · .txt de até 5 MB e 20.000 caracteres",
    help: "Lê até 20.000 caracteres de forma contínua, respeitando parágrafos e pontuação.",
    button: "Gerar narração contínua",
    resultTitle: "Sua narração contínua está pronta",
    downloadName: "narration_continuous.mp3",
    footnote: "O texto é narrado de ponta a ponta, sem pausas definidas por timestamps.",
    messages: [
      "Enviando e validando o TXT…",
      "Preparando o texto para leitura…",
      "Gerando a narração em português…",
      "Montando o arquivo MP3…",
    ],
  },
};

let selectedFile = null;
let objectUrl = null;
let timerId = null;
let startedAt = 0;

function getMode() {
  const value = form.querySelector('input[name="narrationMode"]:checked')?.value;
  return MODES[value] ?? MODES.subtitle;
}

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

function applyMode() {
  const mode = getMode();
  clearError();
  revokeResult();
  setSelectedFile(null);

  fileInput.name = mode.fieldName;
  fileInput.accept = mode.accept;
  fileLegendText.textContent = mode.fileLegend;
  dropPrompt.textContent = mode.dropPrompt;
  dropHint.textContent = mode.dropHint;
  modeHelp.textContent = mode.help;
  selectedFileType.textContent = mode.fileType;
  generateButtonText.textContent = mode.button;
  resultTitle.textContent = mode.resultTitle;
  downloadButton.download = mode.downloadName;
  footnoteText.textContent = mode.footnote;
}

function setSelectedFile(file) {
  const mode = getMode();
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

  if (!file.name.toLowerCase().endsWith(mode.extension)) {
    setSelectedFile(null);
    showError(`Escolha um arquivo com a extensão ${mode.extension}.`);
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
  selectedFileType.textContent = mode.fileType;
  selectedFileName.textContent = file.name;
  selectedFileSize.textContent = `${formatFileSize(file.size)} · pronto para gerar`;
  dropZone.hidden = true;
  selectedFilePanel.hidden = false;
  generateButton.disabled = false;
}

function setProcessing(active) {
  const mode = getMode();
  form.setAttribute("aria-busy", String(active));
  generateButton.classList.toggle("is-loading", active);
  generateButton.disabled = active || !selectedFile;
  processingPanel.hidden = !active;

  fileInput.disabled = active;
  removeFileButton.disabled = active;
  document.querySelectorAll('input[name="voice"]').forEach((input) => {
    input.disabled = active;
  });
  modeInputs.forEach((input) => {
    input.disabled = active;
  });

  if (!active) {
    window.clearInterval(timerId);
    timerId = null;
    return;
  }

  startedAt = Date.now();
  elapsedTime.textContent = "00:00";
  processingStage.textContent = mode.messages[0];
  timerId = window.setInterval(() => {
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    elapsedTime.textContent = formatElapsed(seconds);
    processingStage.textContent = mode.messages[Math.floor(seconds / 8) % mode.messages.length];
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

modeInputs.forEach((input) => {
  input.addEventListener("change", applyMode);
});

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
  const mode = getMode();

  if (!selectedFile) {
    showError(`Selecione um arquivo ${mode.fileType} antes de continuar.`);
    return;
  }

  const voice = form.querySelector('input[name="voice"]:checked')?.value;
  if (!voice) {
    showError("Selecione uma voz para a narração.");
    return;
  }

  const payload = new FormData();
  payload.set(mode.fieldName, selectedFile, selectedFile.name);
  payload.set("voice", voice);
  setProcessing(true);

  try {
    const response = await fetch(mode.endpoint, {
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
    downloadButton.download = mode.downloadName;
    resultTitle.textContent = mode.resultTitle;
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

applyMode();
