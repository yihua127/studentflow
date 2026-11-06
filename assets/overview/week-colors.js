export default function ({data, parentElement, setTriggerValue}) {
  parentElement.innerHTML = data;
  const palette = parentElement.querySelector('.sf-week-palette');
  const calendar = parentElement.closest('.st-key-sf_panel_overview_calendar');
  let marker;

  function handleClick(event) {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.matches('.sf-week-marker')) {
      if (palette.matches(':popover-open')) palette.hidePopover();
      marker = button;
      palette.showPopover({source: button});
      const box = button.getBoundingClientRect();
      palette.style.left = Math.max(8, Math.min(box.right + 6, window.innerWidth - palette.offsetWidth - 8)) + 'px';
      palette.style.top = Math.max(8, Math.min(box.top, window.innerHeight - palette.offsetHeight - 8)) + 'px';
      palette.querySelector('[data-color="' + button.dataset.currentColor + '"]').focus();
    } else if (palette.contains(button) && button.dataset.color && marker) {
      palette.hidePopover();
      marker.focus();
      setTriggerValue('color', {task_id: marker.dataset.taskId, color: button.dataset.color});
    }
  }

  calendar?.addEventListener('click', handleClick);
  return () => calendar?.removeEventListener('click', handleClick);
}
