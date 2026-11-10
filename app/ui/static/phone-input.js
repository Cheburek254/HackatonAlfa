function formatRussianPhone(value) {
    const raw = value.trim();
    if (raw === "+" || /^\+(?!7)\d/.test(raw)) {
        return raw;
    }
    const digits = raw.replace(/[^0-9]/g, "");
    if (!digits) {
        return "";
    }
    const local = (digits.length === 10 && !raw.startsWith("+")
        ? digits
        : digits.replace(/^[78]/, "")).slice(0, 10);
    let formatted = "+7";
    if (local.length) formatted += ` (${local.slice(0, 3)}`;
    if (local.length >= 3) formatted += ")";
    if (local.length > 3) formatted += ` ${local.slice(3, 6)}`;
    if (local.length > 6) formatted += `-${local.slice(6, 8)}`;
    if (local.length > 8) formatted += `-${local.slice(8)}`;
    return formatted;
}

for (const input of document.querySelectorAll("[data-russian-phone]")) {
    if (/^\+7[0-9]{10}$/.test(input.value)) {
        input.value = formatRussianPhone(input.value);
    }
    input.addEventListener("input", (event) => {
        const value = input.value;
        const cursor = input.selectionStart;
        if (event.inputType === "deleteContentBackward" && value === "+" && cursor === 1) {
            input.value = "";
            return;
        }
        if (event.inputType === "deleteContentBackward" && cursor === value.length && /^\+7 \(\d{3}$/.test(value)) {
            input.value = value.slice(0, -1);
            return;
        }
        const formatted = formatRussianPhone(value);
        if (formatted === value) {
            return;
        }
        const suffixDigits = value.slice(cursor).replace(/[^0-9]/g, "").length;
        input.value = formatted;
        let position = formatted.length;
        for (let remaining = suffixDigits; remaining > 0 && position > 0;) {
            position -= 1;
            if (/\d/.test(formatted[position])) remaining -= 1;
        }
        input.setSelectionRange(position, position);
    });
}
