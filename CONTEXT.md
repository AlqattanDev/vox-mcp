# Vox

Local-only voice I/O between a person and the coding agents they work with: the
agent speaks through the machine's own speakers, the person answers into its own
microphone, and nothing leaves the machine.

## Language

### The turn

**Utterance**:
One continuous stretch of agent speech, produced by one call. Several in a row
play back to back rather than merging into one.
_Avoid_: message, announcement

**Listen**:
The window in which the microphone is open and the person's speech is being
captured. A listen belongs to a turn; it is not a mode the session sits in.
_Avoid_: recording, capture window

**Barge-in**:
The person starting to speak while an utterance is still playing, which ends the
utterance and treats what they said as their answer. Distinct from taking the
floor: barge-in cuts the agent off, taking the floor does not.
_Avoid_: interrupt, talk-over

**Cancel**:
The person stopping an utterance with no intention of answering it. Reserved for
"stop, that was wrong". The explicit Cancel control has this meaning; the ⌘§
turn key is not Cancel when an agent utterance is playing.
_Avoid_: interrupt, abort

### The floor

**The floor**:
The right to speak next, held either by the agent or by the person. While an
utterance plays the agent holds it; whether it passes to the person afterwards is
what the floor indicator shows.

**Take the floor**:
The person claiming the microphone for after the current utterance, when the
agent had not planned to offer it. Does not stop the utterance and is not an
error condition.
_Avoid_: interrupt, request to speak, raise hand

**Decline the floor**:
The person waving off a microphone the agent had planned to offer, so the agent
carries straight on with the work.
_Avoid_: skip, mute, cancel

**Floor indicator**:
The part of the on-screen pill that says whether the person will get the
microphone when the current utterance ends. Two states only: they will, or they
will not.

**Floor override**:
The record that the person, rather than the agent, decided who holds the floor.
Reported to the agent beside what was said, never folded into the transcript
text.

### Delivery

**Reply**:
An answer the person starts themselves from the panel, outside any turn the agent
opened. It reaches the agent as a note, not as the result of a call.
_Avoid_: response, callback

**Note**:
Something the person said that no open call was waiting for, held until the
agent's next action delivers it.
_Avoid_: message, buffer, pending

**Dictation**:
Speech turned into text and typed into whatever app has focus, with no agent
involved.
_Avoid_: transcription, speech-to-text
