(() => {
  const rules = document.getElementById('explore-rules');
  document.getElementById('add-explore-rule').addEventListener('click', () => {
    if (rules.children.length >= 20) return;
    rules.append(document.getElementById('explore-rule-template').content.cloneNode(true));
  });
  rules.addEventListener('click', event => {
    if (event.target.closest('[data-remove-rule]')) event.target.closest('.filter-rule').remove();
  });
  const updateValue = row => {
    const source = row.querySelector('[name=rule_field]').value === '__source_connection__';
    const operator = row.querySelector('[name=rule_op]');
    [...operator.options].forEach(option => { option.hidden = source && !['eq', 'ne'].includes(option.value); });
    if (source && !['eq', 'ne'].includes(operator.value)) operator.value = 'eq';
    const missing = ['is_null', 'not_null'].includes(operator.value);
    const value = row.querySelector('[name=rule_value]');
    const selection = row.querySelector('[data-source-rule-values]');
    value.hidden = source;
    selection.hidden = !source;
    value.readOnly = missing;
    if (source) value.value = JSON.stringify([...selection.selectedOptions].map(option => option.value));
    else if (missing) value.value = '';
    value.placeholder = missing ? 'No value needed' : 'Text, number or date';
  };
  rules.addEventListener('change', event => {
    const row = event.target.closest('.filter-rule');
    if (!row) return;
    if (event.target.name === 'rule_field' && event.target.value !== '__source_connection__') row.querySelector('[name=rule_value]').value = '';
    updateValue(row);
  });
  [...rules.children].forEach(updateValue);
  const profiles = JSON.parse(document.getElementById('explore-source-profiles').textContent);
  const dialog = document.getElementById('source-connection-detail');
  document.querySelectorAll('[data-source-uuid]').forEach(button => button.addEventListener('click', () => {
    const content = document.getElementById('source-connection-content');
    content.replaceChildren();
    const entries = profiles.filter(profile => profile.server_uuid === button.dataset.sourceUuid || (profile.server_uuids || []).includes(button.dataset.sourceUuid));
    const show = (label, value, list) => {
      const term = document.createElement('dt'), description = document.createElement('dd');
      term.textContent = label; description.textContent = value || '—'; list.append(term, description);
    };
    const snapshot = document.createElement('dl');
    show('Server UUID', button.dataset.sourceUuid, snapshot);
    show('Connection when archived', button.dataset.sourceName, snapshot);
    show('Source table', button.dataset.sourceTable, snapshot);
    show('Server hostname when archived', button.dataset.sourceHostname, snapshot);
    content.append(snapshot);
    entries.forEach(profile => {
      const list = document.createElement('dl');
      show('Registered connection', profile.name, list);
      show('Host / socket', profile.socket || `${profile.host}:${profile.port || 3306}`, list);
      show('User', profile.user, list);
      show('Observed instance UUIDs', (profile.server_uuids || [profile.server_uuid]).join(', '), list);
      content.append(list);
    });
    if (!entries.length) {
      const message = document.createElement('p');
      message.textContent = 'No currently registered source connection matches this UUID.';
      content.append(message);
    }
    dialog.showModal();
  }));
})();
