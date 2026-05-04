const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  AlignmentType, HeadingLevel, BorderStyle, WidthType, ShadingType,
  VerticalAlign, LevelFormat
} = require("docx");
const fs = require("fs");

const border = { style: BorderStyle.SINGLE, size: 1, color: "CCCCCC" };
const borders = { top: border, bottom: border, left: border, right: border };
const headerBorder = { style: BorderStyle.SINGLE, size: 1, color: "2E75B6" };
const headerBorders = { top: headerBorder, bottom: headerBorder, left: headerBorder, right: headerBorder };

function heading1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 360, after: 120 },
    children: [new TextRun({ text, bold: true, size: 28, color: "2E75B6", font: "Arial" })]
  });
}

function heading2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 240, after: 80 },
    children: [new TextRun({ text, bold: true, size: 24, color: "1F4E79", font: "Arial" })]
  });
}

function bodyText(text) {
  return new Paragraph({
    spacing: { before: 60, after: 60 },
    children: [new TextRun({ text, size: 22, font: "Arial" })]
  });
}

function note(text) {
  return new Paragraph({
    spacing: { before: 60, after: 60 },
    indent: { left: 360 },
    children: [new TextRun({ text: `\u26a0\ufe0f  ${text}`, size: 20, italics: true, color: "7F7F7F", font: "Arial" })]
  });
}

function checklistTable(rows) {
  return new Table({
    width: { size: 9360, type: WidthType.DXA },
    columnWidths: [600, 5760, 3000],
    rows: [
      new TableRow({
        tableHeader: true,
        children: [
          new TableCell({
            borders: headerBorders,
            width: { size: 600, type: WidthType.DXA },
            shading: { fill: "2E75B6", type: ShadingType.CLEAR },
            margins: { top: 80, bottom: 80, left: 120, right: 120 },
            verticalAlign: VerticalAlign.CENTER,
            children: [new Paragraph({ children: [new TextRun({ text: "", bold: true, size: 22, color: "FFFFFF", font: "Arial" })] })]
          }),
          new TableCell({
            borders: headerBorders,
            width: { size: 5760, type: WidthType.DXA },
            shading: { fill: "2E75B6", type: ShadingType.CLEAR },
            margins: { top: 80, bottom: 80, left: 120, right: 120 },
            verticalAlign: VerticalAlign.CENTER,
            children: [new Paragraph({ children: [new TextRun({ text: "Task", bold: true, size: 22, color: "FFFFFF", font: "Arial" })] })]
          }),
          new TableCell({
            borders: headerBorders,
            width: { size: 3000, type: WidthType.DXA },
            shading: { fill: "2E75B6", type: ShadingType.CLEAR },
            margins: { top: 80, bottom: 80, left: 120, right: 120 },
            verticalAlign: VerticalAlign.CENTER,
            children: [new Paragraph({ children: [new TextRun({ text: "Notes", bold: true, size: 22, color: "FFFFFF", font: "Arial" })] })]
          }),
        ]
      }),
      ...rows.map((row, i) =>
        new TableRow({
          children: [
            new TableCell({
              borders,
              width: { size: 600, type: WidthType.DXA },
              shading: { fill: i % 2 === 0 ? "FFFFFF" : "F5F9FF", type: ShadingType.CLEAR },
              margins: { top: 80, bottom: 80, left: 120, right: 120 },
              verticalAlign: VerticalAlign.CENTER,
              children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: "\u2610", size: 24, font: "Arial" })] })]
            }),
            new TableCell({
              borders,
              width: { size: 5760, type: WidthType.DXA },
              shading: { fill: i % 2 === 0 ? "FFFFFF" : "F5F9FF", type: ShadingType.CLEAR },
              margins: { top: 80, bottom: 80, left: 120, right: 120 },
              children: [new Paragraph({ children: [new TextRun({ text: row.task, size: 22, bold: row.bold || false, font: "Arial" })] })]
            }),
            new TableCell({
              borders,
              width: { size: 3000, type: WidthType.DXA },
              shading: { fill: i % 2 === 0 ? "FFFFFF" : "F5F9FF", type: ShadingType.CLEAR },
              margins: { top: 80, bottom: 80, left: 120, right: 120 },
              children: [new Paragraph({ children: [new TextRun({ text: row.notes || "", size: 20, italics: true, color: "595959", font: "Arial" })] })]
            }),
          ]
        })
      )
    ]
  });
}

function spacer() {
  return new Paragraph({ spacing: { before: 120, after: 120 }, children: [new TextRun("")] });
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: "Arial", size: 22 } } }
  },
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [{
          level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } }
        }]
      }
    ]
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 }
      }
    },
    children: [
      // Title
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { before: 0, after: 80 },
        children: [new TextRun({ text: "AWS Deployment Checklist", bold: true, size: 48, color: "1F4E79", font: "Arial" })]
      }),
      new Paragraph({
        alignment: AlignmentType.CENTER,
        spacing: { before: 0, after: 400 },
        children: [new TextRun({ text: "usdm_to_osb  \u2014  USDM 4.0 to OpenStudyBuilder", size: 24, color: "595959", font: "Arial" })]
      }),

      // SECTION 1
      heading1("Step 1 \u2014 Launch an EC2 Instance"),
      spacer(),
      checklistTable([
        { task: "Log in to AWS Console and go to EC2", notes: "console.aws.amazon.com" },
        { task: "Click Launch Instance", notes: "" },
        { task: "Choose AMI: Amazon Linux 2023 or Ubuntu 22.04", notes: "Either works fine" },
        { task: "Choose instance type: t3.small or larger", notes: "t3.micro may be tight on memory" },
        { task: "Configure Security Group: allow SSH (port 22) from your IP", notes: "Restrict to your IP for security" },
        { task: "Ensure outbound internet access is enabled", notes: "Needed to reach OSB API" },
        { task: "Download or select your key pair (.pem file)", notes: "Keep this safe \u2014 cannot recover it" },
        { task: "Launch the instance and note the Public IP address", notes: "" },
      ]),
      spacer(),

      // SECTION 2
      heading1("Step 2 \u2014 Connect & Install Python"),
      spacer(),
      checklistTable([
        { task: "SSH into your EC2 instance", notes: "ssh -i key.pem ec2-user@YOUR_IP" },
        { task: "Install Python 3, pip, and git (Amazon Linux)", notes: "sudo yum install python3 python3-pip git -y" },
        { task: "Install Python 3, pip, and git (Ubuntu)", notes: "sudo apt update && sudo apt install python3 python3-pip git -y" },
        { task: "Verify Python version is 3.10 or higher", notes: "python3 --version" },
      ]),
      spacer(),

      // SECTION 3
      heading1("Step 3 \u2014 Clone the Repository"),
      spacer(),
      checklistTable([
        { task: "Clone repo from GitHub", notes: "git clone https://github.com/YOUR_USERNAME/usdm_to_osb.git" },
        { task: "Change into the repo folder", notes: "cd usdm_to_osb" },
        { task: "Confirm epoch_mapping.csv is present in the folder", notes: "ls epoch_mapping.csv" },
      ]),
      spacer(),

      // SECTION 4
      heading1("Step 4 \u2014 Install Python Dependencies"),
      spacer(),
      checklistTable([
        { task: "Install all dependencies from requirements.txt", notes: "pip3 install -r requirements.txt" },
        { task: "Verify requests is installed", notes: "python3 -c \"import requests\"" },
        { task: "Verify pandas is installed", notes: "python3 -c \"import pandas\"" },
        { task: "Verify beautifulsoup4 is installed", notes: "python3 -c \"from bs4 import BeautifulSoup\"" },
      ]),
      note("If you see 'No module named X', re-run: pip3 install -r requirements.txt"),
      spacer(),

      // SECTION 5
      heading1("Step 5 \u2014 Create config.json on the Server"),
      spacer(),
      bodyText("This file contains your credentials and is NOT in the GitHub repo (it is gitignored for security). You must create it manually on the server."),
      spacer(),
      checklistTable([
        { task: "Create config.json in the repo folder", notes: "nano config.json" },
        { task: "Add api_base_url (your OSB instance URL)", notes: "e.g. https://your-osb/api" },
        { task: "Add idp_url (your OAuth2 identity provider URL)", notes: "e.g. https://your-osb" },
        { task: "Add client_id", notes: "Usually: osbidp" },
        { task: "Add client_secret", notes: "Your OSB client secret" },
        { task: "Add username", notes: "Your OSB login email" },
        { task: "Add password", notes: "Your OSB login password" },
        { task: "Add project_number", notes: "e.g. CDISC DEV" },
        { task: "Save the file (Ctrl+O, Enter, Ctrl+X in nano)", notes: "" },
        { task: "Confirm config.json is NOT tracked by git", notes: "git status should NOT show config.json" },
      ]),
      note("Use config_template.json in the repo as a guide for the correct format."),
      spacer(),

      // SECTION 6
      heading1("Step 6 \u2014 Upload Your USDM Study File"),
      spacer(),
      checklistTable([
        { task: "Upload your USDM .json file to the server", notes: "scp -i key.pem study.json ec2-user@YOUR_IP:~/usdm_to_osb/" },
        { task: "Confirm the file is in the repo folder", notes: "ls *.json" },
      ]),
      spacer(),

      // SECTION 7
      heading1("Step 7 \u2014 Run the Script"),
      spacer(),
      checklistTable([
        { task: "Run validation first (no API connection needed)", notes: "python3 -m usdm_to_osb validate your_study.json", bold: true },
        { task: "Confirm validation output shows PASSED", notes: "Fix any errors before uploading" },
        { task: "Run the upload", notes: "python3 -m usdm_to_osb upload your_study.json --config config.json", bold: true },
        { task: "Confirm Study UID is printed in output", notes: "Means upload was successful" },
        { task: "Check the log file for any warnings or errors", notes: "usdm_upload_YYYYMMDD_HHMMSS.log" },
      ]),
      spacer(),

      // SECTION 8 - Quick ref
      heading1("Quick Reference"),
      spacer(),
      heading2("Useful Commands"),
      spacer(),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [3600, 5760],
        rows: [
          new TableRow({
            tableHeader: true,
            children: [
              new TableCell({
                borders: headerBorders, width: { size: 3600, type: WidthType.DXA },
                shading: { fill: "1F4E79", type: ShadingType.CLEAR },
                margins: { top: 80, bottom: 80, left: 120, right: 120 },
                children: [new Paragraph({ children: [new TextRun({ text: "Action", bold: true, size: 22, color: "FFFFFF", font: "Arial" })] })]
              }),
              new TableCell({
                borders: headerBorders, width: { size: 5760, type: WidthType.DXA },
                shading: { fill: "1F4E79", type: ShadingType.CLEAR },
                margins: { top: 80, bottom: 80, left: 120, right: 120 },
                children: [new Paragraph({ children: [new TextRun({ text: "Command", bold: true, size: 22, color: "FFFFFF", font: "Arial" })] })]
              }),
            ]
          }),
          ...[
            ["Validate USDM file", "python3 -m usdm_to_osb validate study.json"],
            ["Upload to OSB", "python3 -m usdm_to_osb upload study.json --config config.json"],
            ["Skip specific sections", "... --skip arms epochs"],
            ["List all codelists", "python3 -m usdm_to_osb list-codelists --config config.json"],
            ["View log file", "cat usdm_upload_*.log"],
            ["Pull latest code from GitHub", "git pull origin main"],
          ].map(([action, cmd], i) =>
            new TableRow({
              children: [
                new TableCell({
                  borders, width: { size: 3600, type: WidthType.DXA },
                  shading: { fill: i % 2 === 0 ? "FFFFFF" : "F5F9FF", type: ShadingType.CLEAR },
                  margins: { top: 80, bottom: 80, left: 120, right: 120 },
                  children: [new Paragraph({ children: [new TextRun({ text: action, size: 22, font: "Arial" })] })]
                }),
                new TableCell({
                  borders, width: { size: 5760, type: WidthType.DXA },
                  shading: { fill: i % 2 === 0 ? "FFFFFF" : "F5F9FF", type: ShadingType.CLEAR },
                  margins: { top: 80, bottom: 80, left: 120, right: 120 },
                  children: [new Paragraph({ children: [new TextRun({ text: cmd, size: 20, font: "Courier New", color: "1F4E79" })] })]
                }),
              ]
            })
          )
        ]
      }),
      spacer(),
      heading2("Files That Must Exist on the Server"),
      spacer(),
      new Table({
        width: { size: 9360, type: WidthType.DXA },
        columnWidths: [2880, 2880, 3600],
        rows: [
          new TableRow({
            tableHeader: true,
            children: [
              new TableCell({
                borders: headerBorders, width: { size: 2880, type: WidthType.DXA },
                shading: { fill: "1F4E79", type: ShadingType.CLEAR },
                margins: { top: 80, bottom: 80, left: 120, right: 120 },
                children: [new Paragraph({ children: [new TextRun({ text: "File", bold: true, size: 22, color: "FFFFFF", font: "Arial" })] })]
              }),
              new TableCell({
                borders: headerBorders, width: { size: 2880, type: WidthType.DXA },
                shading: { fill: "1F4E79", type: ShadingType.CLEAR },
                margins: { top: 80, bottom: 80, left: 120, right: 120 },
                children: [new Paragraph({ children: [new TextRun({ text: "Source", bold: true, size: 22, color: "FFFFFF", font: "Arial" })] })]
              }),
              new TableCell({
                borders: headerBorders, width: { size: 3600, type: WidthType.DXA },
                shading: { fill: "1F4E79", type: ShadingType.CLEAR },
                margins: { top: 80, bottom: 80, left: 120, right: 120 },
                children: [new Paragraph({ children: [new TextRun({ text: "Notes", bold: true, size: 22, color: "FFFFFF", font: "Arial" })] })]
              }),
            ]
          }),
          ...[
            ["epoch_mapping.csv", "GitHub repo", "Auto-present after git clone"],
            ["config.json", "Create manually", "Never commit \u2014 contains passwords"],
            ["your_study.json", "Upload via scp", "Your USDM 4.0 study file"],
            ["requirements.txt", "GitHub repo", "Used to install dependencies"],
          ].map(([file, source, notes], i) =>
            new TableRow({
              children: [
                new TableCell({
                  borders, width: { size: 2880, type: WidthType.DXA },
                  shading: { fill: i % 2 === 0 ? "FFFFFF" : "F5F9FF", type: ShadingType.CLEAR },
                  margins: { top: 80, bottom: 80, left: 120, right: 120 },
                  children: [new Paragraph({ children: [new TextRun({ text: file, size: 20, font: "Courier New", color: "1F4E79" })] })]
                }),
                new TableCell({
                  borders, width: { size: 2880, type: WidthType.DXA },
                  shading: { fill: i % 2 === 0 ? "FFFFFF" : "F5F9FF", type: ShadingType.CLEAR },
                  margins: { top: 80, bottom: 80, left: 120, right: 120 },
                  children: [new Paragraph({ children: [new TextRun({ text: source, size: 22, font: "Arial" })] })]
                }),
                new TableCell({
                  borders, width: { size: 3600, type: WidthType.DXA },
                  shading: { fill: i % 2 === 0 ? "FFFFFF" : "F5F9FF", type: ShadingType.CLEAR },
                  margins: { top: 80, bottom: 80, left: 120, right: 120 },
                  children: [new Paragraph({ children: [new TextRun({ text: notes, size: 20, italics: true, color: "595959", font: "Arial" })] })]
                }),
              ]
            })
          )
        ]
      }),
      spacer(),
    ]
  }]
});

Packer.toBuffer(doc).then(buffer => {
  fs.writeFileSync("AWS_Deployment_Checklist.docx", buffer);
  console.log("Created: AWS_Deployment_Checklist.docx");
});
