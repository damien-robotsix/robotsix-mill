# Attaching screenshots

When you file a ticket from the web board, you can attach a single
screenshot to it. The refine agent then uses that image as visual
context while it turns your draft into a spec — handy for bug reports,
UI tweaks, or anything that is easier to show than to describe.

## How to attach a screenshot

Screenshots are attached from the **New Ticket** modal on the board, at
the moment you create the ticket. There are two ways to do it:

1. **File picker** — click the **Screenshot** field and choose an image
   file from your computer.
2. **Paste from clipboard** — copy an image (for example with a
   screen-capture tool) and paste it directly into the **Description**
   textarea. The board detects the pasted image and fills in the
   Screenshot field for you.

One screenshot is attached per ticket through the modal. If you both
pick a file *and* paste an image, the file you explicitly chose wins —
a paste only fills the Screenshot field when it is still empty.

!!! note

    Drag-and-drop is not supported, and the modal attaches a single
    screenshot per ticket. To add more visual context, put it in the
    Description.

## Supported formats

The Screenshot field accepts these image formats:

- **PNG**
- **JPEG**
- **GIF**
- **WebP**

If you upload anything that is not one of these image types, the board
rejects it with an HTTP `400` error and the message
`upload must be an image (png, jpeg, gif, webp)`.

## Best practices

- **Crop to the relevant region.** A tight screenshot of the area that
  matters is easier for the agent to interpret than a full-screen
  capture.
- **Prefer PNG for UI screenshots.** It stays crisp for text and
  interface elements.
- **Keep files reasonably small.** There is no hard size limit enforced
  by the application, but very large uploads are bounded only by the
  HTTP server or reverse proxy in front of the board, so a smaller image
  is the safer choice.
- **Use the Description for anything the image can't convey.** The
  screenshot is context, not a substitute for explaining what you want.

## What happens to your screenshot

Your screenshot is stored alongside the ticket and preserved across
refine restarts — even if refinement starts over from scratch, your
image is kept.

During refinement, the agent uses the screenshot as visual context
where the backend supports it. On a vision-capable backend the agent can
see the image; on a text-only backend the screenshot is still preserved,
just not visually interpreted.
