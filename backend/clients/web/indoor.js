"use strict";

function fileHint() {
  const status = document.getElementById("status");
  if (status) {
    status.textContent = "Serve /app/indoor over HTTP to edit the map.";
  }
}

async function loadGraph() {
  const resp = await fetch("/v1/indoor/graph", { headers: { Accept: "application/json" } });
  if (!resp.ok) throw new Error("graph " + resp.status);
  return resp.json();
}

function renderRooms(graph) {
  const list = document.getElementById("rooms");
  if (!list) return;
  list.innerHTML = (graph.nodes || [])
    .map((node) => "<li>" + String(node.name || "") + "</li>")
    .join("");
}

function boot() {
  if (window.location.protocol === "file:") {
    fileHint();
    return;
  }
  const status = document.getElementById("status");
  loadGraph()
    .then((graph) => {
      renderRooms(graph);
      if (status) {
        status.textContent = (graph.nodes || []).length
          ? "Rooms on this floor."
          : "No indoor map yet. Add rooms.";
      }
    })
    .catch(() => {
      if (status) status.textContent = "Could not load the indoor graph.";
    });

  const addRoom = document.getElementById("add-room");
  if (addRoom) {
    addRoom.addEventListener("submit", (event) => {
      event.preventDefault();
      const name = document.getElementById("room-name").value;
      fetch("/v1/indoor/nodes", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      }).then(() => loadGraph().then(renderRooms));
    });
  }

  const route = document.getElementById("route");
  if (route) {
    route.addEventListener("submit", (event) => {
      event.preventDefault();
      const toRoom = document.getElementById("to-target").value;
      fetch("/v1/gateway/tools", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: "indoor_route", arguments: { to_room: toRoom } }),
      })
        .then((resp) => resp.json())
        .then((body) => {
          const out = document.getElementById("route-out");
          if (out) out.textContent = (body.result && body.result.spoken) || body.error || "";
        });
    });
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
