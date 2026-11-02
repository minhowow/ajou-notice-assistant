// 아주대 학교 계정에서 실행하는 연결 스크립트.
// Google Cloud 프로젝트 없이 캘린더와 메일을 다룬다. 메일은 읽기 전용 권한이라 보낼 수 없다.
// __SECRET__ 은 setup 스크립트가 실제 값으로 바꾼다. 이 파일을 공유하지 않는다.
const SECRET = '__SECRET__';
const VERSION = 2;
const MAX_MAILS = 30;
const CALENDAR = 'primary';

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (error) {
    return reply({ ok: false, error: 'bad request' });
  }
  if (body.secret !== SECRET) return reply({ ok: false, error: 'forbidden' });
  const action = ACTIONS[body.action];
  if (!action) return reply({ ok: false, error: 'unknown action' });
  try {
    return reply({ ok: true, result: action(body) });
  } catch (error) {
    return reply({ ok: false, error: String(error) });
  }
}

function reply(value) {
  return ContentService.createTextOutput(JSON.stringify(value)).setMimeType(ContentService.MimeType.JSON);
}

function me() {
  return Session.getEffectiveUser().getEmail();
}

// 캘린더 API 일정을 봇이 쓰는 모양으로 줄인다. status는 내 참석 응답이다.
function eventRow(event) {
  const self = (event.attendees || []).find((person) => person.self || person.email === me());
  const props = (event.extendedProperties && event.extendedProperties.private) || {};
  return {
    id: event.id, title: event.summary || '', link: event.htmlLink || '',
    start: event.start.dateTime || event.start.date, end: event.end.dateTime || event.end.date,
    allDay: !event.start.dateTime, key: props.ajou_key || '', status: self ? self.responseStatus : '',
  };
}

function findByKey(key) {
  const found = Calendar.Events.list(CALENDAR, { privateExtendedProperty: 'ajou_key=' + key, maxResults: 1 }).items || [];
  return found.length ? found[0] : null;
}

const ACTIONS = {
  ping: () => ({ account: me(), calendar: CalendarApp.getDefaultCalendar().getId(), version: VERSION }),

  calendar_list: (body) => (Calendar.Events.list(CALENDAR, {
    timeMin: new Date(body.from).toISOString(), timeMax: new Date(body.to).toISOString(),
    singleEvents: true, orderBy: 'startTime', maxResults: 250,
  }).items || []).map(eventRow),

  // 같은 key로 두 번 호출해도 일정은 하나만 생긴다. rsvp면 나를 참석자로 넣어 예/아니요/미정을 고를 수 있게 한다.
  calendar_add: (body) => {
    const found = findByKey(body.key);
    if (found) return { created: false, event: eventRow(found) };
    const when = (value) => (body.allDay ? { date: value.slice(0, 10) } : { dateTime: value, timeZone: 'Asia/Seoul' });
    const resource = {
      summary: body.title, description: body.notes || '',
      start: when(body.start), end: when(body.end),
      reminders: { useDefault: false, overrides: (body.reminders || []).map((minutes) => ({ method: 'popup', minutes })) },
      extendedProperties: { private: { ajou_key: body.key } },
    };
    if (body.rsvp) resource.attendees = [{ email: me(), responseStatus: body.status || 'needsAction' }];
    if (body.color) resource.colorId = String(body.color);
    return { created: true, event: eventRow(Calendar.Events.insert(resource, CALENDAR, { sendUpdates: 'none' })) };
  },

  // 내 참석 응답만 바꾼다. 다른 참석자와 일정 내용은 건드리지 않는다.
  calendar_rsvp: (body) => {
    const allowed = ['accepted', 'declined', 'tentative', 'needsAction'];
    if (allowed.indexOf(body.status) < 0) throw new Error('bad status');
    const event = Calendar.Events.get(CALENDAR, body.id);
    const attendees = event.attendees || [];
    const self = attendees.find((person) => person.self || person.email === me());
    if (self) self.responseStatus = body.status;
    else attendees.push({ email: me(), responseStatus: body.status });
    return eventRow(Calendar.Events.patch({ attendees }, CALENDAR, body.id, { sendUpdates: 'none' }));
  },

  // 비서가 만든 일정(ajou_key가 있는 일정)만 지운다. 사용자가 만든 일정은 지울 수 없다.
  calendar_remove: (body) => {
    const event = Calendar.Events.get(CALENDAR, body.id);
    const props = (event.extendedProperties && event.extendedProperties.private) || {};
    // 1판이 만든 일정은 CalendarApp 태그로 표시돼 있다. 태그가 확장 속성에 안 보이면 태그로 다시 확인한다.
    const legacy = !props.ajou_key && event.iCalUID ? CalendarApp.getDefaultCalendar().getEventById(event.iCalUID) : null;
    if (!props.ajou_key && !(legacy && legacy.getTag('ajou_key'))) throw new Error('not created by assistant');
    Calendar.Events.remove(CALENDAR, body.id, { sendUpdates: 'none' });
    return { removed: true, id: body.id };
  },

  mail_search: (body) => {
    const limit = Math.min(body.limit || 10, MAX_MAILS);
    const listed = Gmail.Users.Messages.list('me', { q: body.query, maxResults: limit }).messages || [];
    return listed.map((row) => {
      const message = Gmail.Users.Messages.get('me', row.id, { format: 'full' });
      const header = (name) => (message.payload.headers.find((item) => item.name.toLowerCase() === name) || {}).value || '';
      return {
        id: message.id, from: header('from'), subject: header('subject'), date: header('date'),
        snippet: message.snippet, body: plainText(message.payload).slice(0, 20000),
        attachments: attachmentNames(message.payload),
      };
    });
  },
};

function plainText(part) {
  if (part.mimeType === 'text/plain' && part.body && part.body.data) {
    return Utilities.newBlob(Utilities.base64DecodeWebSafe(part.body.data)).getDataAsString('UTF-8');
  }
  return (part.parts || []).map(plainText).join('\n');
}

function attachmentNames(part) {
  const own = part.filename ? [part.filename] : [];
  return own.concat(...(part.parts || []).map(attachmentNames));
}
