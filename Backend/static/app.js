function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

function matchTierClass(percentage) {
  if (percentage >= 75) return "match-high";
  if (percentage >= 40) return "match-mid";
  return "match-low";
}

function renderEmptyState() {
  return `
    <div class="empty-state">
      <div class="glyph">&#10003;</div>
      <strong>No matching paths found</strong>
      This path appears to be unique in the existing records.
    </div>
  `;
}

function renderError(message) {
  return `<div class="error-state">${escapeHtml(message)}</div>`;
}

// Keep only <mark> tags from a backend snippet; strip everything else.
function sanitizeSnippet(html) {
  return (html ?? "").replace(/<(?!\/?mark\b)[^>]*>/gi, "");
}

function renderResults(data) {
  const matches = data?.data?.matches || [];
  if (matches.length === 0) {
    return renderEmptyState();
  }

  let html = `
    <div class="result-summary">
      <h3>Matching Paths</h3>
      <span class="count">${matches.length}</span>
    </div>
  `;

  matches.forEach((item) => {
    const score = Math.round(item.confidence_score);
    const tier = matchTierClass(score);
    const meta = [item.date, item.topic].filter(Boolean).map(escapeHtml).join(" &middot; ");

    html += `
      <div class="card">
        <div class="card-top">
          <div class="card-path">${sanitizeSnippet(item.matched_snippet)}</div>
          <span class="match-badge ${tier}">${score}% match</span>
        </div>
        ${meta ? `<div class="matched-words"><span class="label">${meta}</span></div>` : ""}
      </div>
    `;
  });

  return html;
}

async function checkPath() {
  const input = document.getElementById("pathInput");
  const resultEl = document.getElementById("result");
  const btn = document.getElementById("checkBtn");
  const path = input.value.trim();

  if (!path) {
    resultEl.innerHTML = renderError("Please enter a Brahmavidya Path before checking.");
    return;
  }

  const originalLabel = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner"></span>Checking...`;

  try {
    const response = await fetch("/check", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ path: path }),
    });

    if (!response.ok) {
      throw new Error(`Request failed (${response.status})`);
    }

    const data = await response.json();

    if (data.status === "error") {
      resultEl.innerHTML = renderError(data.message || "Something went wrong. Please try again.");
      return;
    }

    resultEl.innerHTML = renderResults(data);
  } catch (err) {
    resultEl.innerHTML = renderError(
      "Could not reach the server. Please check your connection and try again."
    );
  } finally {
    btn.disabled = false;
    btn.innerHTML = originalLabel;
  }
}

document.getElementById("pathInput")?.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    checkPath();
  }
});

async function addPath() {

  const input =
    document.getElementById(
      "pathInput"
    );

  const path =
    input.value.trim();

  const resultEl =
    document.getElementById(
      "result"
    );

  const btn =
    document.getElementById(
      "addBtn"
    );

  if (!path) {

    resultEl.innerHTML =
      renderError(
        "Please enter a Brahmavidya Path before adding."
      );

    return;
  }

  const originalLabel =
    btn.innerHTML;

  btn.disabled = true;

  btn.innerHTML =
    `<span class="spinner"></span>Adding...`;

  try {

    const response =
      await fetch(
        "/add",
        {
          method: "POST",

          headers: {
            "Content-Type":
              "application/json"
          },

          body: JSON.stringify({
            path
          })
        }
      );

    const data =
      await response.json();

    if (data.status === "error") {

      resultEl.innerHTML =
        renderError(
          data.message ||
          "Unable to add path."
        );

      return;
    }

    resultEl.innerHTML = `
      <div class="empty-state">
        <div class="glyph">&#10003;</div>
        <strong>Path Added Successfully</strong>
        ${escapeHtml(path)}
      </div>
    `;

    input.value = "";

  }
  catch {

    resultEl.innerHTML =
      renderError(
        "Could not reach the server. Please try again."
      );
  }
  finally {

    btn.disabled = false;

    btn.innerHTML =
      originalLabel;
  }
}