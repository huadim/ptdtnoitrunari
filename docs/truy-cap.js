"use strict";

const register = new URLSearchParams(window.location.search).get("mode") === "register";
const action = register ? "đăng ký" : "đăng nhập";
const selectedTab = document.getElementById(register ? "register-tab" : "login-tab");
selectedTab.setAttribute("aria-current", "page");
document.title = (register ? "Đăng ký" : "Đăng nhập") + " · EDUSCHOOL Manager";
document.getElementById("access-title").textContent = register ? "Đăng ký tài khoản giáo viên" : "Đăng nhập vào trường";
document.getElementById("access-description").textContent = register
  ? "Mở ứng dụng của trường rồi nhập mã mời do hiệu trưởng cấp để tự tạo tài khoản."
  : "Nhập địa chỉ ứng dụng do nhà trường cung cấp để mở trang đăng nhập.";
document.getElementById("access-submit").firstChild.textContent = "Mở trang " + action + " ";

document.getElementById("access-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const error = document.getElementById("access-error");
  const input = document.getElementById("school-url");
  let destination;
  try {
    destination = new URL(input.value.trim());
    if (destination.protocol !== "https:" || destination.username || destination.password ||
        destination.search || destination.hash || destination.pathname !== "/") {
      throw new Error("invalid_address");
    }
    if (destination.origin === window.location.origin) {
      throw new Error("static_site");
    }
  } catch {
    error.textContent = "Nhập địa chỉ gốc HTTPS do trường cấp, ví dụ https://giaovu.truong.edu.vn.";
    error.hidden = false;
    input.setAttribute("aria-invalid", "true");
    input.focus();
    return;
  }
  error.hidden = true;
  input.removeAttribute("aria-invalid");
  destination.pathname = register ? "/dang-ky/" : "/dang-nhap/";
  window.location.assign(destination.href);
});
