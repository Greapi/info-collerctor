/* Apply the saved theme before the stylesheet paints to avoid a light flash. */
(function () {
  try {
    var stored = localStorage.getItem('configUiTheme');
    if (stored === 'dark' || stored === 'light') {
      document.documentElement.dataset.theme = stored;
    }
  } catch (error) {
    /* localStorage can be unavailable in hardened browser profiles. */
  }
})();
