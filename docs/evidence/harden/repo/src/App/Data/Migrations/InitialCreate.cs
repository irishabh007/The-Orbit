using System;
using Microsoft.EntityFrameworkCore.Infrastructure;
using Microsoft.EntityFrameworkCore.Migrations;

namespace App.Data.Migrations;

[DbContext(typeof(AppDbContext))]
[Migration("20260101000000_InitialCreate")]
public partial class InitialCreate : Migration
{
    protected override void Up(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.CreateTable(
            name: "ShortLinks",
            columns: table => new
            {
                Id = table.Column<long>(type: "INTEGER", nullable: false)
                    .Annotation("Sqlite:Autoincrement", true),
                Code = table.Column<string>(type: "TEXT", maxLength: 64, nullable: false),
                OriginalUrl = table.Column<string>(type: "TEXT", nullable: false),
                ClickCount = table.Column<long>(type: "INTEGER", nullable: false, defaultValue: 0L),
                CreatedAtUtc = table.Column<DateTime>(type: "TEXT", nullable: false),
                LastVisitedAtUtc = table.Column<DateTime>(type: "TEXT", nullable: true),
                ExpiresAtUtc = table.Column<DateTime>(type: "TEXT", nullable: true)
            },
            constraints: table =>
            {
                table.PrimaryKey("PK_ShortLinks", x => x.Id);
            });

        migrationBuilder.CreateTable(
            name: "ShortLinkDailyClicks",
            columns: table => new
            {
                Id = table.Column<long>(type: "INTEGER", nullable: false)
                    .Annotation("Sqlite:Autoincrement", true),
                ShortLinkId = table.Column<long>(type: "INTEGER", nullable: false),
                Date = table.Column<DateOnly>(type: "TEXT", nullable: false),
                Clicks = table.Column<long>(type: "INTEGER", nullable: false, defaultValue: 0L)
            },
            constraints: table =>
            {
                table.PrimaryKey("PK_ShortLinkDailyClicks", x => x.Id);
                table.ForeignKey(
                    name: "FK_ShortLinkDailyClicks_ShortLinks_ShortLinkId",
                    column: x => x.ShortLinkId,
                    principalTable: "ShortLinks",
                    principalColumn: "Id",
                    onDelete: ReferentialAction.Cascade);
            });

        migrationBuilder.CreateIndex(
            name: "IX_ShortLinks_Code",
            table: "ShortLinks",
            column: "Code",
            unique: true);

        migrationBuilder.CreateIndex(
            name: "IX_ShortLinks_ExpiresAtUtc",
            table: "ShortLinks",
            column: "ExpiresAtUtc");

        migrationBuilder.CreateIndex(
            name: "IX_ShortLinkDailyClicks_ShortLinkId_Date",
            table: "ShortLinkDailyClicks",
            columns: new[] { "ShortLinkId", "Date" },
            unique: true);
    }

    protected override void Down(MigrationBuilder migrationBuilder)
    {
        migrationBuilder.DropTable(
            name: "ShortLinkDailyClicks");

        migrationBuilder.DropTable(
            name: "ShortLinks");
    }
}