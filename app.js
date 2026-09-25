const flows = {
  task: {
    eyebrow: "CATALOG / 结构定位",
    title: "先知道，自己在哪里。",
    copy: "从路径、标题和别名定位已有笔记，再查看目录、入口与显式链接。换一次对话，也有重新出发的位置。",
    steps: ["Projects / 示例项目", "项目主页与目录结构", "已有链接与相关入口"],
    labels: ["目标", "位置", "线索"],
  },
  edit: {
    eyebrow: "RETRIEVAL / 跨目录发现",
    title: "相关知识，不只在同一目录。",
    copy: "结合语义与关键词检索，找到概念说明、书籍材料和项目实现。结果按来源汇总，保留命中章节。",
    steps: ["Concepts / 原理说明", "Sources / 参考材料", "Projects / 应用实现"],
    labels: ["概念", "来源", "应用"],
  },
  conclude: {
    eyebrow: "SOURCE / 按需阅读",
    title: "打开来源，再形成判断。",
    copy: "选择来源后，读取当前 Markdown 的相关小节。检索摘要用于找到材料；解释和结论以原文及其适用条件为依据。",
    steps: ["选择来源与命中章节", "读取当前原文与必要上下文", "核对依据，保留未决问题"],
    labels: ["选择", "阅读", "判断"],
  },
};

const tabs = [...document.querySelectorAll("[data-flow]")];
const panel = document.querySelector("#flow-panel");
const eyebrow = document.querySelector("#flow-eyebrow");
const title = document.querySelector("#flow-title");
const copy = document.querySelector("#flow-copy");
const steps = document.querySelector("#flow-steps");

for (const tab of tabs) {
  tab.addEventListener("click", () => {
    const flow = flows[tab.dataset.flow];
    for (const item of tabs) {
      const selected = item === tab;
      item.classList.toggle("active", selected);
      item.setAttribute("aria-selected", String(selected));
      item.tabIndex = selected ? 0 : -1;
    }
    panel.setAttribute("aria-labelledby", tab.id);
    eyebrow.textContent = flow.eyebrow;
    title.textContent = flow.title;
    copy.textContent = flow.copy;
    steps.replaceChildren(...flow.steps.map((step, index) => {
      const item = document.createElement("li");
      const number = document.createElement("span");
      number.textContent = flow.labels[index];
      item.append(number, step);
      return item;
    }));
  });

  tab.addEventListener("keydown", (event) => {
    const current = tabs.indexOf(tab);
    const target = event.key === "ArrowRight"
      ? tabs[(current + 1) % tabs.length]
      : event.key === "ArrowLeft"
        ? tabs[(current - 1 + tabs.length) % tabs.length]
        : event.key === "Home"
          ? tabs[0]
          : event.key === "End"
            ? tabs[tabs.length - 1]
            : null;
    if (target) {
      event.preventDefault();
      target.focus();
      target.click();
    }
  });
}

const copyButton = document.querySelector("#copy-quickstart");
const copyStatus = document.querySelector("#copy-status");
copyButton.addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(document.querySelector("#quickstart-code").textContent.trim());
    copyButton.textContent = "已复制";
    copyStatus.textContent = "命令已复制到剪贴板。";
    window.setTimeout(() => {
      copyButton.textContent = "复制";
      copyStatus.textContent = "";
    }, 1800);
  } catch {
    copyStatus.textContent = "浏览器未授予剪贴板权限，请手动选择命令。";
  }
});
