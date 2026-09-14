// Older versions: lists the releases (or plain tags) of the repository from the
// GitHub API and points the download button at the chosen version's zip.
(function () {
  "use strict";
  var box = document.getElementById("versions");
  if (!box) return;
  var repo = box.getAttribute("data-repo");
  var select = document.getElementById("version-select");
  var button = document.getElementById("version-download");
  var note = document.getElementById("version-note");
  var api = "https://api.github.com/repos/" + repo;
  var headers = { Accept: "application/vnd.github+json" };

  function zipFor(tag) { return "https://github.com/" + repo + "/archive/refs/tags/" + encodeURIComponent(tag) + ".zip"; }
  function show(text) { note.textContent = text; }
  function versionKey(tag) {
    var m = /(\d+)\.(\d+)/.exec(tag);
    return m ? Number(m[1]) * 100000 + Number(m[2]) : -1;
  }
  function newestFirst(items) {
    return items.slice().sort(function (a, b) { return versionKey(b.tag) - versionKey(a.tag); });
  }
  function fromReleases(list) {
    return list.filter(function (r) { return !r.draft && r.tag_name; }).map(function (r) {
      var label = r.tag_name;
      if (r.name && r.name !== r.tag_name) label += " - " + r.name;
      if (r.published_at) label += " (" + r.published_at.slice(0, 10) + ")";
      return { tag: r.tag_name, label: label };
    });
  }
  function fromTags(list) {
    return list.map(function (t) { return { tag: t.name, label: t.name }; });
  }
  function fetchJson(url) {
    return fetch(url, { headers: headers }).then(function (r) { return r.ok ? r.json() : Promise.reject(new Error("HTTP " + r.status)); });
  }
  function render(items) {
    select.innerHTML = "";
    items.forEach(function (item) {
      var option = document.createElement("option");
      option.value = item.tag;
      option.textContent = item.label;
      select.appendChild(option);
    });
    select.disabled = false;
    button.href = zipFor(items[0].tag);
    button.classList.remove("disabled");
    button.removeAttribute("aria-disabled");
    select.addEventListener("change", function () { button.href = zipFor(select.value); });
    show(items.length + (items.length === 1 ? " version" : " versions") + " published. The newest is listed first; pick one and download it.");
  }

  show("Looking up published versions…");
  fetchJson(api + "/releases?per_page=100")
    .then(function (list) {
      var items = newestFirst(fromReleases(list));
      if (items.length) return items;
      return fetchJson(api + "/tags?per_page=100").then(function (tags) { return newestFirst(fromTags(tags)); });
    })
    .then(function (items) {
      if (items.length) render(items);
      else show("No older versions have been published yet. The latest build above is the only one.");
    })
    .catch(function () {
      show("Could not reach GitHub to list older versions. They are on the releases page linked below.");
    });
})();
