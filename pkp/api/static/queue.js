window.queueApp = function queueApp() {
  return {
    mode: "single",
    batch: {
      filter: "",
      focusedId: null,
      selected: [],
    },
    toast: {
      visible: false,
      message: "",
      timer: null,
    },
    showToast(message) {
      this.toast.message = message;
      this.toast.visible = true;

      if (this.toast.timer) {
        clearTimeout(this.toast.timer);
      }

      this.toast.timer = setTimeout(() => {
        this.toast.visible = false;
      }, 3000);
    },
    resetBatch() {
      this.batch.filter = "";
      this.batch.focusedId = null;
      this.batch.selected = [];
    },
    isEditableTarget(target) {
      if (!target) {
        return false;
      }

      const tagName = target.tagName;
      return (
        target.isContentEditable ||
        tagName === "INPUT" ||
        tagName === "SELECT" ||
        tagName === "TEXTAREA" ||
        tagName === "BUTTON" ||
        tagName === "A"
      );
    },
    handleKeyboardShortcut(event) {
      if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) {
        return;
      }

      if (this.isEditableTarget(event.target)) {
        return;
      }

      if (this.mode === "single") {
        this.handleSingleShortcut(event);
        return;
      }

      if (this.mode === "batch") {
        this.handleBatchShortcut(event);
      }
    },
    switchToSingleMode() {
      const shouldRefresh = this.mode === "batch";
      this.mode = "single";
      if (shouldRefresh) {
        window.dispatchEvent(new Event("refresh-single-queue"));
      }
    },
    handleSingleShortcut(event) {
      const shortcuts = {
        a: "#approve-current",
        r: "#reject-current",
        s: "#skip-current",
      };
      const selector = shortcuts[event.key.toLowerCase()];
      if (!selector) {
        return;
      }

      const button = document.querySelector(selector);
      if (!button || button.disabled) {
        return;
      }

      event.preventDefault();
      button.click();
    },
    handleBatchShortcut(event) {
      if (event.key === "j") {
        event.preventDefault();
        this.focusBatchRow(1);
      }

      if (event.key === "k") {
        event.preventDefault();
        this.focusBatchRow(-1);
      }
    },
    normalizedFilter() {
      return this.batch.filter.trim().toLowerCase();
    },
    batchRowVisible(row) {
      const filter = this.normalizedFilter();
      if (!filter) {
        return true;
      }

      return row.dataset.title.includes(filter);
    },
    visibleBatchIds() {
      const rows = Array.from(document.querySelectorAll("[data-batch-row]"));
      return rows
        .filter((row) => this.batchRowVisible(row))
        .map((row) => row.dataset.proposalId);
    },
    focusBatchRow(direction) {
      const visibleIds = this.visibleBatchIds();
      if (visibleIds.length === 0) {
        this.batch.focusedId = null;
        return;
      }

      const currentIndex = visibleIds.indexOf(this.batch.focusedId);
      let nextIndex = currentIndex + direction;
      if (currentIndex === -1) {
        nextIndex = direction > 0 ? 0 : visibleIds.length - 1;
      }

      if (nextIndex < 0) {
        nextIndex = visibleIds.length - 1;
      }

      if (nextIndex >= visibleIds.length) {
        nextIndex = 0;
      }

      this.batch.focusedId = visibleIds[nextIndex];
      this.$nextTick(() => {
        const row = document.querySelector(
          `[data-batch-row][data-proposal-id="${this.batch.focusedId}"]`,
        );
        row?.scrollIntoView({ block: "nearest" });
      });
    },
    isSelected(proposalId) {
      return this.batch.selected.includes(proposalId);
    },
    toggleSelected(proposalId, checked) {
      if (checked) {
        if (!this.isSelected(proposalId)) {
          this.batch.selected.push(proposalId);
        }
        return;
      }

      this.batch.selected = this.batch.selected.filter((id) => id !== proposalId);
    },
    allVisibleSelected() {
      const visibleIds = this.visibleBatchIds();
      return (
        visibleIds.length > 0 &&
        visibleIds.every((proposalId) => this.isSelected(proposalId))
      );
    },
    toggleAllVisible(checked) {
      const visibleIds = this.visibleBatchIds();
      if (!checked) {
        this.batch.selected = this.batch.selected.filter(
          (proposalId) => !visibleIds.includes(proposalId),
        );
        return;
      }

      const selected = new Set(this.batch.selected);
      visibleIds.forEach((proposalId) => selected.add(proposalId));
      this.batch.selected = Array.from(selected);
    },
  };
};
