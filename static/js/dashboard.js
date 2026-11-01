(function () {
  function data(id) { var el = document.getElementById(id); return el ? JSON.parse(JSON.parse(el.textContent)) : null; }
  var s = data("series-data");
  if (s && window.Chart) {
    new Chart(document.getElementById("activityChart"), {
      type: "bar",
      data: { labels: s.labels.map(function (d) { return d.slice(5); }), datasets: [
        { label: "E-mails sent", data: s.sent, backgroundColor: "#4e79a7" },
        { label: "Replies", data: s.replies, backgroundColor: "#59a14f" },
        { type: "line", label: "New companies", data: s.leads, borderColor: "#f28e2b", backgroundColor: "#f28e2b", tension: 0.3 }
      ]},
      options: { responsive: true, plugins: { legend: { position: "bottom" } }, scales: { y: { beginAtZero: true, ticks: { precision: 0 } } } }
    });
  }
  var f = data("funnel-data");
  if (f && window.Chart) {
    new Chart(document.getElementById("funnelChart"), {
      type: "bar",
      data: { labels: f.map(function (x) { return x.label; }), datasets: [{ label: "Companies", data: f.map(function (x) { return x.count; }), backgroundColor: "#0b5394" }] },
      options: { indexAxis: "y", plugins: { legend: { display: false } }, scales: { x: { beginAtZero: true, ticks: { precision: 0 } } } }
    });
  }
})();
