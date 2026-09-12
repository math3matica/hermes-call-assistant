# Manual acceptance checklist

These checks require a configured provider, audio devices, and optionally a
physical phone. They were not established by the automated test suite.

- [ ] Launch the separate terminal Call Assistant runtime; confirm it does
      not alter Hermes built-in `/voice`.
- [ ] Verify model supervisor startup, readiness, and failure recovery.
- [ ] Verify STT, TTS, microphone capture, and output routing independently.
- [ ] Verify Bluetooth/handset audio in both directions without host-speaker
      loopback or unintended sidetone.
- [ ] Verify one authorized Phone Bridge integration session and terminal
      cleanup; do not treat a dial command as a connected call.
- [ ] Verify post-call artifacts and conservative promotion behavior.
- [ ] In a real conversation, verify bounded Shared Knowledge/Prepared Briefing
      retrieval and distinguish it from context merely being offered.

Record provider/model, route, timestamps, artifacts, and human audio findings
separately. Do not claim PASS from process existence or model narration alone.