document.querySelectorAll('[data-open-modal]').forEach(button => {
  button.addEventListener('click', () => document.getElementById(button.dataset.openModal)?.showModal());
});
document.querySelectorAll('[data-close-modal]').forEach(button => {
  button.addEventListener('click', () => button.closest('dialog')?.close());
});
document.querySelectorAll('dialog').forEach(dialog => {
  dialog.addEventListener('click', event => {
    if (event.target === dialog) dialog.close();
  });
});
document.querySelectorAll('[data-dismiss-toast]').forEach(button => {
  button.addEventListener('click', () => button.closest('.toast')?.remove());
});
document.querySelectorAll('[data-password-toggle]').forEach(button => {
  button.addEventListener('click', () => {
    const input = button.parentElement.querySelector('input');
    const visible = input.type === 'password';
    input.type = visible ? 'text' : 'password';
    button.setAttribute('aria-pressed', String(visible));
    button.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
    button.querySelector('i')?.classList.toggle('fa-eye', !visible);
    button.querySelector('i')?.classList.toggle('fa-eye-slash', visible);
  });
});
document.querySelectorAll('form.application-form').forEach(form => {
  const applicantType = form.querySelector('[name="applicant_type"]');
  const gradeFields = form.querySelector('[data-academic-grades]');
  const rowTemplate = form.querySelector('#grade-row-template');
  if (!applicantType || !gradeFields || !rowTemplate) return;
  const syncGradeFields = () => {
    const freshman = applicantType.value === 'freshman';
    gradeFields.hidden = !freshman;
    gradeFields.querySelectorAll('input').forEach(input => { input.required = freshman; });
  };
  applicantType.addEventListener('change', syncGradeFields);
  form.querySelectorAll('[data-add-grade]').forEach(button => button.addEventListener('click', () => {
    const level = button.dataset.addGrade;
    const fragment = rowTemplate.content.cloneNode(true);
    fragment.querySelectorAll('input').forEach(input => { input.name = input.name.replace('__LEVEL__', level); });
    form.querySelector(`[data-grade-rows="${level}"]`).append(fragment);
    syncGradeFields();
  }));
  form.addEventListener('click', event => {
    const remove = event.target.closest('[data-remove-grade]');
    if (remove && form.contains(remove)) remove.closest('.academic-grade-row').remove();
  });
  syncGradeFields();
});
document.querySelectorAll('.toast').forEach(toast => {
  setTimeout(() => toast.remove(), 5500);
});
document.querySelectorAll('[data-confirm]').forEach(form => {
  form.addEventListener('submit', event => {
    if (!window.confirm(form.dataset.confirm)) event.preventDefault();
  });
});
const menuButton = document.querySelector('[data-menu-toggle]');
if (menuButton) menuButton.addEventListener('click', () => document.getElementById('sidebar')?.classList.toggle('open'));
const notificationRoot = document.querySelector('[data-notification-root]');
if (notificationRoot) {
  const toggle = notificationRoot.querySelector('[data-notification-toggle]');
  const panel = notificationRoot.querySelector('#notification-popover');
  const closeNotifications = () => {
    panel.hidden = true;
    toggle.setAttribute('aria-expanded', 'false');
  };
  toggle.addEventListener('click', () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute('aria-expanded', String(!panel.hidden));
  });
  document.addEventListener('click', event => {
    if (!notificationRoot.contains(event.target)) closeNotifications();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && !panel.hidden) {
      closeNotifications();
      toggle.focus();
    }
  });
}
const gradePeriod = document.querySelector('[data-grade-period]');
if (gradePeriod) {
  const fillGrades = () => document.querySelectorAll('[data-grades]').forEach(input => {
    const values = JSON.parse(input.dataset.grades || '{}');
    input.value = values[gradePeriod.value] || '';
  });
  gradePeriod.addEventListener('change', fillGrades);
  fillGrades();
}
