const assert = require("node:assert/strict");
const fs = require("node:fs");
const test = require("node:test");
const vm = require("node:vm");

const script = fs.readFileSync("docs/truy-cap.js", "utf8");

function gateway(mode = "") {
  const elements = new Map();
  let submit;
  let opened;
  const document = {
    getElementById(id) {
      if (!elements.has(id)) {
        elements.set(id, {
          value: "", firstChild: {}, attributes: {}, hidden: true,
          setAttribute(key, value) { this.attributes[key] = value; },
          removeAttribute(key) { delete this.attributes[key]; },
          addEventListener(event, callback) { if (event === "submit") submit = callback; },
          focus() {},
        });
      }
      return elements.get(id);
    },
  };
  vm.runInNewContext(script, {
    URL, URLSearchParams, document,
    window: { location: { origin: "https://huadim.github.io", search: mode, assign(url) { opened = url; } } },
  });
  return {
    open(value) {
      opened = undefined;
      document.getElementById("school-url").value = value;
      submit({ preventDefault() {} });
      return opened;
    },
    elements,
  };
}

test("the school address routes to the correct account entry point", () => {
  assert.equal(gateway().open("https://giaovu.example.edu.vn"), "https://giaovu.example.edu.vn/dang-nhap/");
  assert.equal(gateway("?mode=register").open("https://giaovu.example.edu.vn/"), "https://giaovu.example.edu.vn/dang-ky/");
  assert.equal(gateway("?mode=principal").open("https://giaovu.example.edu.vn/"), "https://giaovu.example.edu.vn/dang-nhap/");
});

test("credentials, queries, fragments, plain HTTP and the public Pages host are rejected", () => {
  for (const address of [
    "http://giaovu.example.edu.vn", "javascript:alert(1)", "https://user:password@giaovu.example.edu.vn",
    "https://giaovu.example.edu.vn/?next=https://example.com", "https://giaovu.example.edu.vn/#token",
    "https://giaovu.example.edu.vn/private", "https://huadim.github.io/", "",
  ]) {
    const page = gateway();
    assert.equal(page.open(address), undefined, address);
    assert.equal(page.elements.get("access-error").hidden, false);
    assert.equal(page.elements.get("school-url").attributes["aria-invalid"], "true");
  }
});

test("public navigation exposes login and registration without collecting passwords", () => {
  const home = fs.readFileSync("docs/index.html", "utf8");
  const access = fs.readFileSync("docs/dang-nhap.html", "utf8");
  assert.match(home, /href="https:\/\/ptdtnoitrunari\.dockhosting\.cloud\/dang-nhap\/"/);
  assert.match(home, /href="https:\/\/ptdtnoitrunari\.dockhosting\.cloud\/dang-ky\/"/);
  assert.doesNotMatch(home, /type="password"/);
  assert.doesNotMatch(access, /type="password"/);
  for (const name of ["style.css", "truy-cap.css", "truy-cap.js"]) {
    assert.ok(fs.existsSync("docs/" + name));
  }
});
