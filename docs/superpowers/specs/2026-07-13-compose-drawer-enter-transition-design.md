# Compose drawer enter transition

## Goal

Opening Compose should slide in from the right with the same leftward transition used by the mail-detail drawer.

## Design

`ComposeView` already shares the `mail-detail-drawer` transition styles. The missing transition is caused by mounting it with `open=true`, leaving no rendered closed state for the browser to animate from.

`HomeView` will keep the Compose view mounted in its closed state, then set `composeOpen` to `true` in the next animation frame. All existing Compose entry points will use this shared opener: the Compose button, resuming a draft, and reopening a scheduled draft.

Closing behavior remains unchanged: set `composeOpen` to `false`, retain the component for the existing 360 ms transition, then unmount it. This preserves draft persistence, AI insertion, and send workflows.

## Testing

Add a focused UI-state test around the shared opener or, if the existing test setup does not expose the view transition state, test the state helper directly. Verify that opening first renders the closed state and moves to the open state on the next animation frame. Run the targeted Vitest file and the production build.
